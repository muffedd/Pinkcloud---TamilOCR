"""OCR fast pass for Pink Cloud.

Two engines behind the same ocr_page() contract:

  gemini  (FAST route)  Gemini vision model (default gemini-3.5-flash-lite).
                        Needs GEMINI_API_KEY. Returns text only, so line
                        bboxes are approximated from the page ink profile.
  sarvam  (HEAVY route) Sarvam Document AI "Digitise" (Sarvam Vision, ta-IN).
                        POST /doc-ai/v1/job/digitise -> poll status ->
                        download-url -> ZIP with metadata/page_NNN.json.
                        Needs SARVAM_API_KEY in the environment.

Routing: ocr_page(img, profile) sends FAST pages to Gemini and HEAVY pages
to Sarvam; if that engine fails the other one is tried, and if both fail
the page gets a marked [stub] line. Without a profile, OCR_ENGINE decides.

Environment:
  OCR_ENGINE        "sarvam" (default) or "gemini".
  GEMINI_API_KEY    Gemini API key. Read at call time, never logged.
  GEMINI_MODEL      default "gemini-3.5-flash-lite".
  GEMINI_LANGUAGE   prompt language name, default "Tamil".
  GEMINI_TIMEOUT_S  per-page HTTP budget, default 120.
  SARVAM_API_KEY    Sarvam API subscription key. Read at call time, never
                    logged, never stored.
  SARVAM_LANGUAGE   default "ta-IN".
  SARVAM_TIMEOUT_S  per-page budget for the whole Sarvam job, default 120.
  SARVAM_POLL_S     status poll interval, default 3.
  SARVAM_BASE_URL   default "https://api.sarvam.ai".

Sarvam returns layout BLOCKS (often a whole paragraph), not lines. Each
block's text is split on newlines and the block box is divided evenly
top-to-bottom, so line bboxes are an approximation and every line carries
its block's confidence (a layout score, not per-line recognition
certainty).

Engine failures are logged (logger.exception) and surfaced in /health via
engine_status(); stub output is never presented as real OCR text.
"""

from __future__ import annotations

import io
import json
import logging
import os
import time
import zipfile

logger = logging.getLogger("pinkcloud.ocr")

# Which engine produced the last page, and the last per-engine failure.
_LAST_ENGINE: str | None = None
_SARVAM_ERROR: str | None = None
_GEMINI_ERROR: str | None = None


def selected_engine() -> str:
    """Engine chosen by OCR_ENGINE (default sarvam)."""
    v = (os.environ.get("OCR_ENGINE") or "sarvam").strip().lower()
    return v if v in ("sarvam", "gemini") else "sarvam"


def warn_legacy_env() -> None:
    """Log once at startup if a removed/invalid engine setting is still set.

    OCR_ENGINE is live again (sarvam|gemini); any other value, and
    SARVAM_FALLBACK, are ignored so an old .env never silently changes
    behaviour."""
    engine = (os.environ.get("OCR_ENGINE") or "").strip().lower()
    if engine and engine not in ("sarvam", "gemini"):
        logger.warning(
            "OCR_ENGINE=%s is ignored: valid values are sarvam|gemini "
            "(failures fall back to the other engine, then marked [stub] lines)",
            engine)
    fallback = (os.environ.get("SARVAM_FALLBACK") or "").strip().lower()
    if fallback:
        logger.warning(
            "SARVAM_FALLBACK=%s is ignored: PaddleOCR was removed; a Sarvam "
            "failure falls back to Gemini, then marked [stub] lines", fallback)


def engine_status() -> dict:
    """Report OCR engine state for /health and diagnostics.

    ocr_engine is the engine that produced the most recent page
    ("gemini" | "sarvam" | "stub"), or, before any page, the engine that
    is ready to run. ocr_engine_selected is what OCR_ENGINE asks for.
    """
    selected = selected_engine()
    if _LAST_ENGINE is not None:
        engine = _LAST_ENGINE
    else:
        # Before any page: the selected engine if keyed. If not, the OTHER
        # keyed engine still means real OCR (the routes cross-fall-back), so
        # report it; "stub" is only honest when neither engine can run.
        other = "gemini" if selected == "sarvam" else "sarvam"
        keyed = {"sarvam": bool(_sarvam_key()), "gemini": bool(_gemini_key())}
        engine = selected if keyed[selected] else (other if keyed[other] else "stub")
    status = {
        "ocr_engine": engine,
        "ocr_engine_selected": selected,
        "gemini_key_set": bool(_gemini_key()),
        "sarvam_key_set": bool(_sarvam_key()),
    }
    if _GEMINI_ERROR:
        status["gemini_error"] = _GEMINI_ERROR
    if _SARVAM_ERROR:
        status["sarvam_error"] = _SARVAM_ERROR
    if engine == "stub":
        status["ocr_error"] = _SARVAM_ERROR or _GEMINI_ERROR or "no OCR engine available"
    return status


# Routing: FAST -> Gemini, HEAVY -> Sarvam. The other engine is the
# fallback; if both fail the page gets a marked [stub] line.
_ROUTES = {"FAST": ("gemini", "sarvam"), "HEAVY": ("sarvam", "gemini")}


def _route(profile: str | None) -> tuple[str, ...]:
    return _ROUTES.get(profile or "", (selected_engine(),))


def ocr_page(img, profile: str | None = None) -> tuple[list[dict], float]:
    """Run OCR on one page image (BGR or grayscale).

    Routing: FAST -> Gemini, HEAVY -> Sarvam; the other engine is the
    fallback. Without a profile, OCR_ENGINE decides.

    Returns (lines, ms) where each line is:
        {"body": str, "bbox": [x, y, w, h], "confidence": float}
    bboxes are on the SAME page image the router scored, so the frontend
    can draw boxes directly.
    """
    global _LAST_ENGINE, _SARVAM_ERROR, _GEMINI_ERROR
    t0 = time.perf_counter()

    for engine in _route(profile):
        try:
            lines = _gemini_ocr(img) if engine == "gemini" else _sarvam_ocr(img)
        except Exception as exc:
            if engine == "gemini":
                _GEMINI_ERROR = _safe_error(exc)
            else:
                _SARVAM_ERROR = _safe_error(exc)
            logger.exception("%s OCR failed", engine.capitalize())
            continue
        if engine == "gemini":
            _GEMINI_ERROR = None
        else:
            _SARVAM_ERROR = None
        _LAST_ENGINE = engine
        return lines, (time.perf_counter() - t0) * 1000.0

    _LAST_ENGINE = "stub"
    return _stub_lines(img), (time.perf_counter() - t0) * 1000.0


# --------------------------------------------------------------------------
# Sarvam Document AI (Digitise)
# Docs: https://docs.sarvam.ai/api/api-guides-tutorials/document-intelligence/overview
#       https://docs.sarvam.ai/api-reference/doc-ai/job/digitise
# --------------------------------------------------------------------------

_SARVAM_TERMINAL = {"completed", "partially_completed", "failed", "rejected"}
_SARVAM_OK = {"completed", "partially_completed"}


class SarvamError(RuntimeError):
    pass


def _sarvam_key() -> str:
    return (os.environ.get("SARVAM_API_KEY") or "").strip()


def _safe_error(exc: Exception) -> str:
    """One-line error text with every API key scrubbed out, just in case."""
    msg = f"{type(exc).__name__}: {exc}".strip().splitlines()[0][:300]
    for key in (_sarvam_key(), _gemini_key()):
        if key:
            msg = msg.replace(key, "***")
    return msg


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def _sarvam_request(client, method: str, url: str, deadline: float, **kw):
    """HTTP call with retry on 429/503 (Sarvam's documented retryable codes)."""
    import httpx

    attempt = 0
    while True:
        try:
            r = client.request(method, url, **kw)
        except httpx.TransportError as exc:
            r, err = None, exc
        else:
            err = None
            if r.status_code not in (429, 503):
                if r.status_code >= 400:
                    raise SarvamError(
                        f"{method} {url} -> HTTP {r.status_code}: {r.text[:200]}"
                    )
                return r
        attempt += 1
        if attempt > 4:
            if err is not None:
                raise SarvamError(f"{method} {url} failed: {err}") from err
            raise SarvamError(f"{method} {url} -> HTTP {r.status_code} after retries")
        wait = 2.0 ** attempt
        if r is not None:
            try:
                wait = max(wait, float(r.headers.get("retry-after", 0)))
            except ValueError:
                pass
        if time.monotonic() + wait > deadline:
            raise SarvamError(f"{method} {url}: out of time while retrying")
        time.sleep(wait)


def _sarvam_ocr(img, client=None) -> list[dict]:
    """Digitise one page image with Sarvam and return contract lines."""
    key = _sarvam_key()
    if not key:
        raise SarvamError("SARVAM_API_KEY is not set")

    import cv2
    import httpx

    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise SarvamError("could not encode page image as PNG")
    h, w = img.shape[:2]

    base = (os.environ.get("SARVAM_BASE_URL") or "https://api.sarvam.ai").rstrip("/")
    lang = os.environ.get("SARVAM_LANGUAGE") or "ta-IN"
    budget = _env_float("SARVAM_TIMEOUT_S", 120.0)
    poll = max(0.0, _env_float("SARVAM_POLL_S", 3.0))
    deadline = time.monotonic() + budget

    own = client is None
    if own:
        client = httpx.Client(timeout=60.0)
    try:
        auth = {"api-subscription-key": key}
        r = _sarvam_request(
            client, "POST", f"{base}/doc-ai/v1/job/digitise", deadline,
            headers=auth,
            files={"file": ("page.png", buf.tobytes(), "image/png")},
            data={"language": lang, "output_format": "md"},
        )
        job_id = r.json().get("job_id")
        if not job_id:
            raise SarvamError("digitise response had no job_id")

        status = ""
        while True:
            st = _sarvam_request(
                client, "GET", f"{base}/doc-ai/v1/job/{job_id}/status", deadline,
                headers=auth,
            ).json()
            status = str(st.get("status", "")).lower()
            if status in _SARVAM_TERMINAL:
                break
            if time.monotonic() + poll > deadline:
                raise SarvamError(f"job {job_id} still '{status}' after {budget:.0f}s")
            time.sleep(poll)
        if status not in _SARVAM_OK:
            raise SarvamError(f"job {job_id} ended with status '{status}'")

        dl = _sarvam_request(
            client, "GET", f"{base}/doc-ai/v1/job/{job_id}/download-url", deadline,
            headers=auth,
        ).json()
        # Presigned URL: send only the headers Sarvam returns, NOT the API key.
        z = _sarvam_request(
            client, (dl.get("method") or "GET").upper(), dl["url"], deadline,
            headers=dl.get("headers") or {},
        )
        page = _page_json_from_zip(z.content)
    finally:
        if own:
            client.close()
    return _parse_sarvam_page(page, w, h)


def _page_json_from_zip(data: bytes) -> dict:
    """Pull the first metadata/page_NNN.json out of a Digitise ZIP."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = sorted(
            n for n in zf.namelist()
            if "/metadata/page_" in "/" + n and n.endswith(".json")
        )
        if not names:
            raise SarvamError("Digitise ZIP has no metadata/page_*.json")
        return json.loads(zf.read(names[0]).decode("utf-8"))


def _parse_sarvam_page(page: dict, img_w: int, img_h: int) -> list[dict]:
    """Convert one Sarvam page JSON into contract lines.

    Kept network-free so it can be unit-tested. Block shape (observed):
      {"coordinates": {"x1","y1","x2","y2"}, "bbox_norm": [x1,y1,x2,y2],
       "layout_tag": str, "confidence": float, "reading_order": int,
       "text": "line one\nline two"}
    Coordinates are rescaled from Sarvam's image_width/height to our image.
    """
    src_w = float(page.get("image_width") or img_w or 1)
    src_h = float(page.get("image_height") or img_h or 1)
    sx, sy = img_w / src_w, img_h / src_h

    blocks = list(page.get("blocks") or [])
    blocks.sort(key=lambda b: (b.get("reading_order") is None, b.get("reading_order") or 0))

    lines: list[dict] = []
    for b in blocks:
        parts = [p.strip() for p in str(b.get("text") or "").split("\n")]
        parts = [p for p in parts if p]
        if not parts:
            continue

        c = b.get("coordinates") or {}
        try:
            if c:
                x1, y1 = float(c["x1"]) * sx, float(c["y1"]) * sy
                x2, y2 = float(c["x2"]) * sx, float(c["y2"]) * sy
            else:
                nx1, ny1, nx2, ny2 = (float(v) for v in b["bbox_norm"])
                x1, y1, x2, y2 = nx1 * img_w, ny1 * img_h, nx2 * img_w, ny2 * img_h
        except (KeyError, TypeError, ValueError):
            logger.warning("unparseable Sarvam block box: %r", b.get("block_id"))
            x1 = y1 = x2 = y2 = 0.0

        try:
            conf = float(b.get("confidence"))
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))

        # A box with no width or no height cannot frame the text; zero it
        # (same "unknown box" marking as an unparseable box). Checking only
        # BOTH dimensions left half-degenerate boxes as bogus 1px slivers.
        degenerate = x2 <= x1 or y2 <= y1
        bw = max(1, int(round(x2 - x1)))
        step = max(0.0, (y2 - y1)) / len(parts)
        for i, text in enumerate(parts):
            if degenerate:
                bbox = [0, 0, 0, 0]
            else:
                ly = int(round(y1 + i * step))
                lh = max(1, int(round(y1 + (i + 1) * step)) - ly)
                bbox = [int(round(x1)), ly, bw, lh]
            lines.append({"body": text, "bbox": bbox, "confidence": conf})
    return lines


# --------------------------------------------------------------------------
# Gemini (fast route)
# Docs: https://ai.google.dev/gemini-api/docs/image-understanding
# --------------------------------------------------------------------------

_GEMINI_DEFAULT_MODEL = "gemini-3.5-flash-lite"
_GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
               "{model}:generateContent")


class GeminiError(RuntimeError):
    pass


def _gemini_key() -> str:
    return (os.environ.get("GEMINI_API_KEY") or "").strip()


def _line_bands(gray, min_h: int = 6, gap: int = 3) -> list[tuple[int, int]]:
    """Horizontal projection-profile text-line bands as (y0, y1).

    ponytail: crude on purpose. Gemini returns text without coordinates,
    so this only exists to give each line an approximate bbox."""
    import numpy as np

    ink = (gray < 128).astype(np.uint8)
    rows = ink.mean(axis=1)
    threshold = max(0.01, float(rows.mean()) * 0.5)
    bands: list[list[int]] = []
    inside, start = False, 0
    for y, value in enumerate(rows):
        if value > threshold and not inside:
            inside, start = True, y
        elif value <= threshold and inside:
            inside = False
            if y - start >= min_h:
                bands.append([start, y])
    if inside and len(rows) - start >= min_h:
        bands.append([start, len(rows)])
    merged: list[list[int]] = []
    for band in bands:
        if merged and band[0] - merged[-1][1] <= gap:
            merged[-1][1] = band[1]
        else:
            merged.append(band)
    return [(b[0], b[1]) for b in merged]


def _lines_with_boxes(img, texts: list[str]) -> list[dict]:
    """Attach an approximate bbox + a text-quality confidence to each line.

    Confidence comes from textcheck.score_line(), the same 0..1 proxy the
    rest of the backend uses (Gemini returns no per-line score)."""
    import cv2

    from .textcheck import score_line

    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    bands = _line_bands(gray)
    if len(bands) == len(texts):
        boxes = [[0, y0, w, y1 - y0] for y0, y1 in bands]
    elif bands:
        top, bottom = bands[0][0], bands[-1][1]
        step = (bottom - top) / len(texts)
        boxes = [[0, int(top + i * step), w, max(1, int(step))]
                 for i in range(len(texts))]
    else:
        step = h / len(texts)
        boxes = [[0, int(i * step), w, max(1, int(step))]
                 for i in range(len(texts))]
    return [{"body": t, "bbox": b, "confidence": score_line(t)}
            for t, b in zip(texts, boxes)]


def _gemini_ocr(img, client=None) -> list[dict]:
    """Transcribe one page with Gemini and return contract lines."""
    key = _gemini_key()
    if not key:
        raise GeminiError("GEMINI_API_KEY is not set")

    import base64
    import cv2
    import httpx

    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise GeminiError("could not encode page image as PNG")
    model = os.environ.get("GEMINI_MODEL") or _GEMINI_DEFAULT_MODEL
    lang = os.environ.get("GEMINI_LANGUAGE") or "Tamil"
    prompt = (f"Transcribe all {lang} text in this image verbatim. "
              "Preserve line breaks. Output only the text, no commentary.")
    body = {
        "contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": "image/png",
                             "data": base64.b64encode(buf.tobytes()).decode()}},
        ]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 8192},
    }

    own = client is None
    if own:
        client = httpx.Client(timeout=_env_float("GEMINI_TIMEOUT_S", 120.0))
    try:
        r = client.post(_GEMINI_URL.format(model=model),
                        headers={"x-goog-api-key": key}, json=body)
        if r.status_code >= 400:
            raise GeminiError(
                f"generateContent -> HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
    finally:
        if own:
            client.close()

    candidate = (data.get("candidates") or [{}])[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(p.get("text") or "" for p in parts)
    texts = [line.strip() for line in text.splitlines() if line.strip()]
    if not texts:
        raise GeminiError(f"no text (finish={candidate.get('finishReason')})")
    return _lines_with_boxes(img, texts)


def failed_page_lines(img, exc: Exception) -> list[dict]:
    """Marked placeholder line for a page whose OCR call raised (any engine).

    Same "[stub]" marking as _stub_lines, so the page is flagged
    needs_review (confidence 0), the text layer / txt / docx exports skip
    it, and the rest of the job still completes. The error text is
    scrubbed of the Sarvam key via _safe_error."""
    h, w = img.shape[:2]
    return [{
        "body": f"[stub] OCR failed on this page - {_safe_error(exc)}",
        "bbox": [int(w * 0.1), int(h * 0.3), int(w * 0.6), int(h * 0.05)],
        "confidence": 0.0,
    }]


def _stub_lines(img) -> list[dict]:
    """Placeholder lines when every OCR engine failed.

    The "[stub]" marker is intentional: stub output must never be
    mistaken for real OCR text (also flagged via needs_review / health).
    """
    h, w = img.shape[:2]
    line_w = int(w * 0.6)
    body = "[stub] OCR unavailable - no engine produced text for this page"
    return [
        {
            "body": body,
            "bbox": [int(w * 0.1), int(h * 0.3), line_w, int(h * 0.05)],
            "confidence": 0.0,
        },
        {
            "body": body,
            "bbox": [int(w * 0.1), int(h * 0.5), line_w, int(h * 0.05)],
            "confidence": 0.0,
        },
    ]