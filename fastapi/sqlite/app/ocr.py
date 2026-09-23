"""OCR fast pass for Pink Cloud.

Uses PaddleOCR with:
  det: PP-OCRv5_mobile_det
  rec: ta_PP-OCRv5_mobile_rec   (Tamil)

paddle is imported LAZILY inside the function, so the whole backend runs
(and passes imports) on machines without paddle installed — we then return
a stub result so the frontend team can build against the real contract.
"""

from __future__ import annotations

import time

# Single global engine; building it takes seconds, so we reuse it.
_ENGINE = None
_ENGINE_INIT_FAILED = False


def _get_engine():
    """Build PaddleOCR once. Returns None if paddle isn't installed."""
    global _ENGINE, _ENGINE_INIT_FAILED
    if _ENGINE is not None or _ENGINE_INIT_FAILED:
        return _ENGINE
    try:
        from paddleocr import PaddleOCR  # lazy import: only if installed

        _ENGINE = PaddleOCR(
            det_model_name="PP-OCRv5_mobile_det",
            rec_model_name="ta_PP-OCRv5_mobile_rec",
            use_textline_orientation=True,
        )
    except Exception:
        # No paddle / no models / wrong version -> run in stub mode.
        _ENGINE = None
        _ENGINE_INIT_FAILED = True
    return _ENGINE


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

    # PaddleOCR 3.x returns one dict per page with rec_texts / rec_boxes.
    results = engine.predict(img)
    lines: list[dict] = []
    for page in results:
        texts = page.get("rec_texts") or []
        scores = page.get("rec_scores") or []
        boxes = page.get("rec_boxes") or page.get("rec_polys") or []
        for i, text in enumerate(texts):
            conf = float(scores[i]) if i < len(scores) else 0.0
            if i < len(boxes):
                box = boxes[i]
                try:
                    # rec_boxes are [x1, y1, x2, y2]; polys are 4 points.
                    if len(box) == 4 and all(isinstance(v, (int, float)) for v in box):
                        x1, y1, x2, y2 = [int(round(v)) for v in box]
                    else:
                        xs = [int(round(p[0])) for p in box]
                        ys = [int(round(p[1])) for p in box]
                        x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
                    bbox = [x1, y1, max(1, x2 - x1), max(1, y2 - y1)]
                except Exception:
                    bbox = [0, 0, 0, 0]
            else:
                bbox = [0, 0, 0, 0]
            lines.append({"body": str(text), "bbox": bbox, "confidence": conf})
    return lines, (time.perf_counter() - t0) * 1000.0


def _stub_lines(img) -> list[dict]:
    """Placeholder lines when paddle is missing.

    Fake two text lines positioned roughly like a scanned page, so the
    output contract and the frontend have something realistic to show.
    """
    h, w = img.shape[:2]
    line_w = int(w * 0.6)
    return [
        {
            "body": "[stub] paddle not installed — OCR fast pass pending",
            "bbox": [int(w * 0.1), int(h * 0.3), line_w, int(h * 0.05)],
            "confidence": 0.0,
        },
        {
            "body": "[stub] paddle not installed — OCR fast pass pending",
            "bbox": [int(w * 0.1), int(h * 0.5), line_w, int(h * 0.05)],
            "confidence": 0.0,
        },
    ]