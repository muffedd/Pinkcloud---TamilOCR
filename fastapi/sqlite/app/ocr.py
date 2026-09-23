"""OCR fast pass for Pink Cloud.

Uses PaddleOCR 3.x with:
  det: PP-OCRv5_mobile_det
  rec: ta_PP-OCRv5_mobile_rec   (Tamil)

paddle is imported LAZILY inside the function, so the whole backend runs
(and passes imports) on machines without paddle installed — we then return
a STUB result, clearly marked, so the app still works end to end.

Engine failures are logged (logger.exception) and surfaced in /health via
engine_status(); stub output is never presented as real OCR text.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time

logger = logging.getLogger("pinkcloud.ocr")

# Single global engine; building it takes seconds, so we reuse it.
_ENGINE = None
_ENGINE_STATE = "not_initialized"  # not_initialized | paddle | stub
_ENGINE_ERROR: str | None = None

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


def engine_status() -> dict:
    """Report OCR engine state for /health and diagnostics."""
    status = {"ocr_engine": _ENGINE_STATE}
    if _ENGINE_ERROR:
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
    t0 = time.perf_counter()
    engine = _get_engine()

    if engine is None:
        return _stub_lines(img), (time.perf_counter() - t0) * 1000.0

    results = engine.predict(img)
    lines = _parse_engine_results(results)
    return lines, (time.perf_counter() - t0) * 1000.0


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