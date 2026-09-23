"""OCR fast pass for Pink Cloud.

Two engines behind the same ocr_page() contract:

  sarvam (default)  Sarvam Document AI "Digitise" (Sarvam Vision, ta-IN).
                    POST /doc-ai/v1/job/digitise -> poll status ->
                    download-url -> ZIP with metadata/page_NNN.json.
                    Needs SARVAM_API_KEY in the environment.
  paddle            Local PaddleOCR 3.x (offline fallback):
                      det: PP-OCRv5_mobile_det
                      rec: ta_PP-OCRv5_mobile_rec   (Tamil)

Environment:
  OCR_ENGINE        "sarvam" (default) or "paddle".
  SARVAM_API_KEY    Sarvam API subscription key. Read at call time, never
                    logged, never stored.
  SARVAM_FALLBACK   "paddle" (default) or "none". When Sarvam is selected
                    but the key is missing or a call fails, "paddle" runs
                    the local engine for that page instead of failing.
  SARVAM_LANGUAGE   default "ta-IN".
  SARVAM_TIMEOUT_S  per-page budget for the whole Sarvam job, default 120.
  SARVAM_POLL_S     status poll interval, default 3.
  SARVAM_BASE_URL   default "https://api.sarvam.ai".

Sarvam returns layout BLOCKS (often a whole paragraph), not lines. Each
block's text is split on newlines and the block box is divided evenly
top-to-bottom, so line bboxes are an approximation and every line carries
its block's confidence (a layout score, not per-line recognition
certainty).

paddle is imported LAZILY inside the function, so the whole backend runs
(and passes imports) on machines without paddle installed — we then return
a STUB result, clearly marked, so the app still works end to end.

Engine failures are logged (logger.exception) and surfaced in /health via
engine_status(); stub output is never presented as real OCR text.
"""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import time
import zipfile

logger = logging.getLogger("pinkcloud.ocr")

# Single global engine; building it takes seconds, so we reuse it.
_ENGINE = None
_ENGINE_STATE = "not_initialized"  # not_initialized | paddle | stub
_ENGINE_ERROR: str | None = None
# Which engine produced the last page, and the last Sarvam failure.
_LAST_ENGINE: str | None = None
_SARVAM_ERROR: str | None = None

# Real PP-OCRv5 settings (verified against paddleocr 3.7.0):
#  - text_detection_model_name / text_recognition_model_name are the 3.x
#    argument names (det_model_name/rec_model_name raise ValueError).
#  - enable_mkldnn=False: paddlepaddle 3.3.1 CPU crashes in predict() with
#    NotImplementedError: ConvertPirAttribute2RuntimeAttribute when MKLDNN
#    is on. It is a "common arg" accepted via **kwargs.
#  - use_doc_orientation_classify / use_doc_unwarping off: unwarping
#    rescales/rewarps the image and breaks the bbox-on-1600px contract.
_ENGINE_KWARGS = dict(
    text_detection_model_name="PP-OCRv5_mobile_det",
    text_recognition_model_name="ta_PP-OCRv5_mobile_rec",
    use_doc_orientation_classify=False,
    use_doc_unwarping=False,
    use_textline_orientation=True,
    enable_mkldnn=False,
)


def _paddle_importable() -> tuple[bool, str]:
    """Probe paddle importability in a SUBPROCESS.

    Official paddlepaddle 3.3.1 wheels require an AVX-capable CPU. On a
    CPU without AVX, importing paddle's native lib hard-crashes the
    process (access violation) — a try/except CANNOT save the server
    from that. Running the import in a child process turns that crash
    into a clean, loggable 'unavailable' and the backend keeps serving
    stub output.
    """
    import importlib.util

    if importlib.util.find_spec("paddle") is None:
        return False, "paddlepaddle is not installed"
    try:
        r = subprocess.run(
            [sys.executable, "-c", "import paddle"],
            capture_output=True,
            timeout=180,
        )
    except Exception as exc:  # probe itself failed — treat as unavailable
        return False, f"paddle probe failed: {exc}"
    if r.returncode == 0:
        return True, ""
    tail = (r.stderr or b"").decode(errors="replace").strip().splitlines()
    reason = tail[-1] if tail else f"paddle import exited with code {r.returncode}"
    return False, f"paddle cannot run on this machine: {reason}"


def _get_engine():
    """Build PaddleOCR once. Returns None if paddle isn't usable."""
    global _ENGINE, _ENGINE_STATE, _ENGINE_ERROR
    if _ENGINE is not None:
        return _ENGINE
    if _ENGINE_STATE == "stub":
        return None
    try:
        # Pre-flight: never import paddle in-process before we know the
        # native lib loads on this CPU (see _paddle_importable).
        ok, reason = _paddle_importable()
        if not ok:
            raise RuntimeError(reason)

        from paddleocr import PaddleOCR  # lazy import: only if installed

        _ENGINE = PaddleOCR(**_ENGINE_KWARGS)
        _ENGINE_STATE = "paddle"
        _ENGINE_ERROR = None
        logger.info("PaddleOCR engine ready: %s", _ENGINE_KWARGS)
    except Exception:
        # A failed engine must be VISIBLE, never silently swallowed.
        _ENGINE = None
        _ENGINE_STATE = "stub"
        import traceback

        _ENGINE_ERROR = traceback.format_exc(limit=3).strip().splitlines()[-1]
        logger.exception("PaddleOCR engine init failed — OCR falls back to STUB output")
    return _ENGINE


def selected_engine() -> str:
    """Engine chosen by OCR_ENGINE (default sarvam)."""
    v = (os.environ.get("OCR_ENGINE") or "sarvam").strip().lower()
    return "paddle" if v == "paddle" else "sarvam"


def _sarvam_fallback() -> str:
    v = (os.environ.get("SARVAM_FALLBACK") or "paddle").strip().lower()
    return "none" if v == "none" else "paddle"


def engine_status() -> dict:
    """Report OCR engine state for /health and diagnostics.

    ocr_engine is the engine that produced the most recent page
    ("sarvam" | "paddle" | "stub"), or, before any page, the engine that
    is ready to run. ocr_engine_selected is what OCR_ENGINE asks for.
    """
    selected = selected_engine()
    if _LAST_ENGINE is not None:
        engine = _LAST_ENGINE
    elif selected == "sarvam" and _sarvam_key():
        engine = "sarvam"
    else:
        engine = _ENGINE_STATE
    status = {"ocr_engine": engine, "ocr_engine_selected": selected}
    if selected == "sarvam":
        status["sarvam_key_set"] = bool(_sarvam_key())
        if _SARVAM_ERROR:
            status["sarvam_error"] = _SARVAM_ERROR
    if _ENGINE_ERROR and engine != "sarvam":
        status["ocr_error"] = _ENGINE_ERROR.strip().splitlines()[-1]
    return status


def _parse_engine_results(results) -> list[dict]:
    """Convert PaddleOCR 3.x predict() output into contract lines.

    Kept paddle-free so it can be unit-tested without paddle installed.
    Handles NumPy output: rec_boxes is a NumPy array (use .tolist()),
    coordinates may be NumPy integers (normalize to plain Python ints).
    """
    lines: list[dict] = []
    for page in results or []:
        texts = page.get("rec_texts") or []
        scores = page.get("rec_scores") or []
        boxes = page.get("rec_boxes")
        if boxes is not None:  # NumPy array of [x1, y1, x2, y2] rows
            boxes = boxes.tolist()
        else:
            boxes = page.get("rec_polys") or []

        for i, text in enumerate(texts):
            conf = float(scores[i]) if i < len(scores) else 0.0
            bbox = [0, 0, 0, 0]
            if i < len(boxes) and boxes[i] is not None:
                box = boxes[i]
                try:
                    if len(box) == 4 and all(
                        isinstance(v, (int, float)) for v in box
                    ):
                        # [x1, y1, x2, y2] — normalize NumPy scalars to ints
                        x1, y1, x2, y2 = (int(round(float(v))) for v in box)
                    else:
                        # 4-point polygon — take its bounding box
                        xs = [int(round(float(p[0]))) for p in box]
                        ys = [int(round(float(p[1]))) for p in box]
                        x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
                    bbox = [x1, y1, max(1, x2 - x1), max(1, y2 - y1)]
                except (TypeError, ValueError, IndexError):
                    logger.warning("unparseable OCR box at index %s: %r", i, box)
                    bbox = [0, 0, 0, 0]
            lines.append({"body": str(text), "bbox": bbox, "confidence": conf})
    return lines


def ocr_page(img) -> tuple[list[dict], float]:
    """Run the fast OCR pass on one page image (BGR or grayscale).

    Returns (lines, ms) where each line is:
        {"body": str, "bbox": [x, y, w, h], "confidence": float}
    bboxes are on the SAME 1600px-capped image the router scored, so the
    frontend can draw boxes directly.
    """
    global _LAST_ENGINE, _SARVAM_ERROR
    t0 = time.perf_counter()

    if selected_engine() == "sarvam":
        try:
            lines = _sarvam_ocr(img)
            _LAST_ENGINE = "sarvam"
            _SARVAM_ERROR = None
            return lines, (time.perf_counter() - t0) * 1000.0
        except Exception as exc:
            _SARVAM_ERROR = _safe_error(exc)
            if _sarvam_fallback() == "none":
                _LAST_ENGINE = "stub"
                logger.exception("Sarvam OCR failed; SARVAM_FALLBACK=none -> STUB output")
                return _stub_lines(img), (time.perf_counter() - t0) * 1000.0
            if _sarvam_key():
                logger.exception("Sarvam OCR failed; falling back to local PaddleOCR")
            else:
                logger.warning("SARVAM_API_KEY not set; using local PaddleOCR")

    lines = _paddle_ocr(img)
    return lines, (time.perf_counter() - t0) * 1000.0


def _paddle_ocr(img) -> list[dict]:
    global _LAST_ENGINE
    engine = _get_engine()
    if engine is None:
        _LAST_ENGINE = "stub"
        return _stub_lines(img)
    results = engine.predict(img)
    _LAST_ENGINE = "paddle"
    return _parse_engine_results(results)


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


def _stub_lines(img) -> list[dict]:
    """Placeholder lines when paddle is missing or failed to initialize.

    The "[stub]" marker is intentional: stub output must never be
    mistaken for real OCR text (also flagged via needs_review / health).
    """
    h, w = img.shape[:2]
    line_w = int(w * 0.6)
    body = "[stub] OCR unavailable - paddle not installed or engine init failed"
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