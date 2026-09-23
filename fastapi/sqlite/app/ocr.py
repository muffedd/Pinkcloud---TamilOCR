"""OCR fast pass for Pink Cloud.

Uses PaddleOCR 3.x (verified against paddleocr 3.7.0 / paddlepaddle 3.3.1):
  det: PP-OCRv5_mobile_det
  rec: ta_PP-OCRv5_mobile_rec   (Tamil)

paddle is imported LAZILY inside the function, so the whole backend runs
(and passes imports) on machines without paddle installed. In that case -
or if the engine fails to build - we return clearly marked STUB lines:
every body starts with "[stub]", confidence is 0.0 (so the page gets
needs_review=true), a warning/error is logged, and engine_status() reports
mode "stub" with the reason so /health can expose it. Stub output must
never be mistaken for real text.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("pinkcloud.ocr")

DET_MODEL = "PP-OCRv5_mobile_det"
REC_MODEL = "ta_PP-OCRv5_mobile_rec"

# Single global engine; building it takes seconds, so we reuse it.
_ENGINE = None
_ENGINE_INIT_FAILED = False
_ENGINE_ERROR: str | None = None


def _build_engine():
    """Construct PaddleOCR 3.x with flags proven on CPU (paddlepaddle 3.3.1)."""
    from paddleocr import PaddleOCR  # lazy import: only if installed

    return PaddleOCR(
        # 3.x names; the 2.x det_model_name / rec_model_name raise ValueError.
        text_detection_model_name=DET_MODEL,
        text_recognition_model_name=REC_MODEL,
        # oneDNN path crashes predict() on paddle 3.3.1 CPU with
        # NotImplementedError: ConvertPirAttribute2RuntimeAttribute.
        enable_mkldnn=False,
        # Doc unwarping changes the coordinate frame, which would break the
        # "bbox is on the 1600px-capped image" contract. Orientation
        # classify is off too (deskew is the router's job; also saves RAM).
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=True,
    )


def _get_engine():
    """Build PaddleOCR once. Returns None (stub mode) if it can't be built."""
    global _ENGINE, _ENGINE_INIT_FAILED, _ENGINE_ERROR
    if _ENGINE is not None or _ENGINE_INIT_FAILED:
        return _ENGINE
    try:
        _ENGINE = _build_engine()
        _ENGINE_ERROR = None
        log.info("PaddleOCR engine ready (det=%s, rec=%s)", DET_MODEL, REC_MODEL)
    except ImportError as exc:
        _ENGINE, _ENGINE_INIT_FAILED = None, True
        _ENGINE_ERROR = f"paddleocr not installed: {exc}"
        log.warning("OCR running in STUB mode - %s", _ENGINE_ERROR)
    except Exception as exc:  # wrong version, missing models, bad flags...
        _ENGINE, _ENGINE_INIT_FAILED = None, True
        _ENGINE_ERROR = f"{type(exc).__name__}: {exc}"
        log.exception("PaddleOCR engine init FAILED - OCR running in STUB mode")
    return _ENGINE


def engine_status() -> dict:
    """Engine mode for /health: {"mode": "paddle"|"stub"|"not_loaded", ...}.

    Does not trigger engine construction (that can take seconds).
    """
    if _ENGINE is not None:
        return {"mode": "paddle", "det": DET_MODEL, "rec": REC_MODEL}
    if _ENGINE_INIT_FAILED:
        return {"mode": "stub", "error": _ENGINE_ERROR}
    return {"mode": "not_loaded"}


def is_stub_line(line: dict) -> bool:
    """True for placeholder lines produced when no OCR engine is available."""
    return str(line.get("body", "")).startswith("[stub]")


def _as_list(v) -> list:
    """numpy array / list / None -> plain list (never truthiness-test arrays)."""
    if v is None:
        return []
    if hasattr(v, "tolist"):
        return v.tolist()
    return list(v)


def _to_bbox(box) -> list[int]:
    """[x1, y1, x2, y2] or a 4-point polygon -> [x, y, w, h] of plain ints."""
    box = _as_list(box)
    if len(box) == 4 and all(not isinstance(v, (list, tuple)) for v in box):
        x1, y1, x2, y2 = [int(round(float(v))) for v in box]
    else:
        pts = [_as_list(p) for p in box]
        xs = [int(round(float(p[0]))) for p in pts]
        ys = [int(round(float(p[1]))) for p in pts]
        x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
    return [x1, y1, max(1, x2 - x1), max(1, y2 - y1)]


def ocr_page(img) -> tuple[list[dict], float]:
    """Run the fast OCR pass on one page image (BGR or grayscale).

    Returns (lines, ms) where each line is:
        {"body": str, "bbox": [x, y, w, h], "confidence": float}
    bboxes are on the SAME 1600px-capped image the router scored, so the
    frontend can draw boxes directly. In stub mode the bodies start with
    "[stub]" and confidence is 0.0.
    """
    t0 = time.perf_counter()
    engine = _get_engine()

    if engine is None:
        return _stub_lines(img), (time.perf_counter() - t0) * 1000.0

    if getattr(img, "ndim", 3) == 2:  # det model expects 3 channels
        import cv2

        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

    # PaddleOCR 3.x returns one result per page with rec_texts / rec_scores /
    # rec_boxes (NumPy int array, [x1, y1, x2, y2]) / rec_polys.
    results = engine.predict(img)
    lines: list[dict] = []
    for page in results:
        texts = _as_list(page.get("rec_texts"))
        scores = _as_list(page.get("rec_scores"))
        boxes = page.get("rec_boxes")
        if boxes is None or len(boxes) == 0:
            boxes = page.get("rec_polys")
        boxes = _as_list(boxes)
        for i, text in enumerate(texts):
            conf = float(scores[i]) if i < len(scores) else 0.0
            bbox = [0, 0, 0, 0]
            if i < len(boxes):
                try:
                    bbox = _to_bbox(boxes[i])
                except Exception:
                    log.exception("could not parse OCR box %r", boxes[i])
            lines.append({"body": str(text), "bbox": bbox, "confidence": conf})
    return lines, (time.perf_counter() - t0) * 1000.0


def _stub_lines(img) -> list[dict]:
    """Placeholder lines when no OCR engine is available.

    Two fake lines positioned roughly like a scanned page so the contract
    and frontend have something to render. Marked "[stub]" + confidence 0.0
    so they are flagged for review and never pass as real OCR text.
    """
    h, w = img.shape[:2]
    line_w = int(w * 0.6)
    body = "[stub] OCR engine unavailable - not real text"
    return [
        {"body": body, "bbox": [int(w * 0.1), int(h * 0.3), line_w, int(h * 0.05)], "confidence": 0.0},
        {"body": body, "bbox": [int(w * 0.1), int(h * 0.5), line_w, int(h * 0.05)], "confidence": 0.0},
    ]
