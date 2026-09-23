"""Output contract builder for Pink Cloud.

Frozen shape (do not add required fields, do not rename) — matches
schema/schema.json (CICT line-based fields):

{
  "page": int,
  "profile": "FAST" | "HEAVY",
  "quality": {"blur": f, "contrast": f, "noise": f, "skew_deg": f},
  "lines": [
     {"id": "L1", "seq": 1, "body": str,
      "bbox": [x, y, w, h], "confidence": 0..1}
  ],
  "text": "<line bodies stitched in seq order>",
  # optional keys
  "needs_review": bool, "processing_ms": int, ...
}

Note: schema.json has additionalProperties:false, so we emit ONLY fields
the schema defines. words[] is optional word-level detail and is NOT
emitted by the fast pass (see schema_out docstring in the repo wiki).
"""

from __future__ import annotations

from typing import Any

# Pages whose best line confidence falls below this go to human review.
# Zero-confidence stub pages and empty OCR results are always flagged.
CONFIDENCE_REVIEW_FLOOR = 0.5


def _reading_sort(ocr_lines: list[dict]) -> list[dict]:
    """Sort lines top-to-bottom, left-to-right WITHIN a y-band.

    A y-band tolerance keeps a right-hand box that sits a few pixels
    higher than its left neighbour in the same visual line (reading
    order must not flip because of a 3px baseline jitter).
    The band height adapts to the median line height (default 12 px).
    """
    if not ocr_lines:
        return []
    heights = sorted(l["bbox"][3] for l in ocr_lines if l["bbox"][3] > 0)
    band = max(12, int(heights[len(heights) // 2] * 0.6)) if heights else 12
    return sorted(ocr_lines, key=lambda l: (round(l["bbox"][1] / band), l["bbox"][0]))


def build_page_result(
    page_number: int,
    profile: str,
    quality: dict[str, float],
    ocr_lines: list[dict],
    processing_ms: float,
) -> dict[str, Any]:
    """Assemble the contract JSON for one page."""
    ordered = _reading_sort(ocr_lines)

    lines = []
    for seq, line in enumerate(ordered, start=1):
        lines.append(
            {
                "id": f"L{seq}",          # stable id: L1, L2, ...
                "seq": seq,                # 1-based reading order
                "body": line["body"],
                "bbox": [int(v) for v in line["bbox"]],
                "confidence": round(float(line["confidence"]), 4),
            }
        )

    # Full page text = every line body stitched in seq order.
    text = "\n".join(line["body"] for line in lines)

    # needs_review is derived from CONFIDENCE, not from the profile alone:
    #  - zero-confidence stub pages (OCR missing/failed) MUST be flagged,
    #  - empty OCR results are unreadable pages -> review,
    #  - real OCR below the floor (e.g. damaged strokes) -> review.
    best_conf = max((line["confidence"] for line in lines), default=0.0)
    needs_review = (
        profile == "HEAVY" or not lines or best_conf < CONFIDENCE_REVIEW_FLOOR
    )

    return {
        "page": int(page_number),
        "profile": profile,  # "FAST" | "HEAVY"
        "quality": {k: round(float(v), 4) for k, v in quality.items()},
        "lines": lines,
        "text": text,
        "needs_review": needs_review,
        "processing_ms": int(round(processing_ms)),
    }


def build_job_result(pages: list[dict]) -> dict[str, Any]:
    """Wrap per-page results into the final job result object."""
    return {"pages": pages}


def parse_job_result(result_json: str | None) -> Any:
    """DB -> JSON object (or None when the job isn't finished yet)."""
    if not result_json:
        return None
    import json

    return json.loads(result_json)