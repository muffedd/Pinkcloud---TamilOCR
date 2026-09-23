"""OCR fast pass for Pink Cloud.

One engine behind the ocr_page() contract:

  sarvam   Sarvam Document AI "Digitise" (Sarvam Vision, ta-IN).
           POST /doc-ai/v1/job/digitise -> poll status ->
           download-url -> ZIP with metadata/page_NNN.json.
           Needs SARVAM_API_KEY in the environment.

If the key is missing or a Sarvam call fails, the page gets clearly
marked "[stub]" lines (confidence 0 -> needs_review) instead of failing
the job. There is no local/offline OCR engine.

Environment:
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

# Which engine produced the last page ("sarvam" | "stub"), and the last
# Sarvam failure (key scrubbed).
_LAST_ENGINE: str | None = None
_SARVAM_ERROR: str | None = None

# Env values from the removed PaddleOCR engine; still honoured as "ignored,
# with a warning" so an old .env never silently changes behaviour.
_LEGACY_ENV = ("OCR_ENGINE", "SARVAM_FALLBACK")


def warn_legacy_env() -> None:
    """Log once at startup if a removed engine setting is still set."""
    for name in _LEGACY_ENV:
        v = (os.environ.get(name) or "").strip().lower()
        if v and v not in ("sarvam", "none"):
            logger.warning(
                "%s=%s is ignored: PaddleOCR was removed, Sarvam is the only "
                "OCR engine (failures fall back to marked [stub] lines)", name, v)


def engine_status() -> dict:
    """Report OCR engine state for /health and diagnostics.

    ocr_engine is the engine that produced the most recent page
    ("sarvam" | "stub"), or, before any page, what the next page will use:
    "sarvam" when a key is set, otherwise "stub".
    """
    if _LAST_ENGINE is not None:
        engine = _LAST_ENGINE
    else:
        engine = "sarvam" if _sarvam_key() else "stub"
    status = {
        "ocr_engine": engine,
        "ocr_engine_selected": "sarvam",
        "sarvam_key_set": bool(_sarvam_key()),
    }
    if _SARVAM_ERROR:
        status["sarvam_error"] = _SARVAM_ERROR
    if engine == "stub":
        status["ocr_error"] = _SARVAM_ERROR or "SARVAM_API_KEY is not set"
    return status


def ocr_page(img) -> tuple[list[dict], float]:
    """Run the fast OCR pass on one page image (BGR or grayscale).

    Returns (lines, ms) where each line is:
        {"body": str, "bbox": [x, y, w, h], "confidence": float}
    bboxes are on the SAME 1600px-capped image the router scored, so the
    frontend can draw boxes directly. On any Sarvam failure (including a
    missing key) the page gets marked "[stub]" lines.
    """
    global _LAST_ENGINE, _SARVAM_ERROR
    t0 = time.perf_counter()
    try:
        lines = _sarvam_ocr(img)
    except Exception as exc:
        _SARVAM_ERROR = _safe_error(exc)
        _LAST_ENGINE = "stub"
        if _sarvam_key():
            logger.exception("Sarvam OCR failed -> STUB output for this page")
        else:
            logger.warning("SARVAM_API_KEY not set -> STUB output for this page")
        return _stub_lines(img), (time.perf_counter() - t0) * 1000.0
    _LAST_ENGINE = "sarvam"
    _SARVAM_ERROR = None
    return lines, (time.perf_counter() - t0) * 1000.0


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
    """One-line error text with the API key scrubbed out, just in case."""
    msg = f"{type(exc).__name__}: {exc}".strip().splitlines()[0][:300]
    key = _sarvam_key()
    return msg.replace(key, "***") if key else msg


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

        bw = max(1, int(round(x2 - x1)))
        step = max(0.0, (y2 - y1)) / len(parts)
        for i, text in enumerate(parts):
            ly = int(round(y1 + i * step))
            lh = max(1, int(round(y1 + (i + 1) * step)) - ly)
            if x2 <= x1 and y2 <= y1:
                bbox = [0, 0, 0, 0]
            else:
                bbox = [int(round(x1)), ly, bw, lh]
            lines.append({"body": text, "bbox": bbox, "confidence": conf})
    return lines


def failed_page_lines(img, exc: Exception) -> list[dict]:
    """Marked placeholder line for a page whose OCR call raised.

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
    """Placeholder lines when Sarvam is unavailable (no key, or the call failed).

    The "[stub]" marker is intentional: stub output must never be
    mistaken for real OCR text (also flagged via needs_review / health).
    """
    h, w = img.shape[:2]
    line_w = int(w * 0.6)
    body = "[stub] OCR unavailable - Sarvam OCR failed or SARVAM_API_KEY not set"
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