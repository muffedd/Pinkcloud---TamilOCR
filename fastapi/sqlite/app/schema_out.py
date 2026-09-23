"""Output contract builder for Pink Cloud.

Frozen shape (do not add required fields, do not rename) — matches
schema/schema.json (CICT line-based fields):

{
  "page": int,
  "profile": "FAST" | "HEAVY",
  "quality": {"blur": f, "contrast": f, "noise": f, "skew_deg": f},
  "lines": [
     {"id": "L1", "seq": 1, "body": str,
      "bbox": [x, y, w, h], "confidence": 0..1,
      "needs_review": bool}
  ],
  "text": "<line bodies stitched in seq order>",
  # optional keys
  "needs_review": bool, "processing_ms": int, "suggestions": [...], ...
}

Note: schema.json has additionalProperties:false, so we emit ONLY fields
the schema defines. words[] is optional word-level detail and is NOT
emitted by the fast pass (see schema_out docstring in the repo wiki).
suggestions[] (page level, shape per schema/suggestions-contract.md) is an
optional passthrough: the HEAVY repair fix-list/dictionary module fills it;
the fast pass emits nothing and the field stays absent until then.
"""

from __future__ import annotations

from typing import Any

# Pages whose best line confidence falls below this go to human review.
# Zero-confidence stub pages and empty OCR results are always flagged.
CONFIDENCE_REVIEW_FLOOR = 0.5

# Marks lines produced by the stub engine (ocr.py) instead of real OCR.
STUB_MARK = "[stub]"


def line_needs_review(line: dict, profile: str | None) -> bool:
    """Per-line review flag: low confidence, stub output, or a HEAVY page.

    Same rule the receipt uses to count human-review lines
    (export._line_needs_human), so GET /jobs/{id} lines and the receipt
    can never disagree."""
    return (
        float(line.get("confidence", 0.0)) < CONFIDENCE_REVIEW_FLOOR
        or str(line.get("body", "")).startswith(STUB_MARK)
        or profile == "HEAVY"
    )


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
    suggestions: list[dict] | None = None,
) -> dict[str, Any]:
    """Assemble the contract JSON for one page.

    `suggestions` is the HEAVY repair fix-list/dictionary output (shape per
    schema/suggestions-contract.md); it is passed through untouched and the
    key is omitted entirely when None, so pages without the module look
    exactly as before."""
    ordered = _reading_sort(ocr_lines)

    lines = []
    for seq, line in enumerate(ordered, start=1):
        entry = {
            "id": f"L{seq}",          # stable id: L1, L2, ...
            "seq": seq,                # 1-based reading order
            "body": line["body"],
            "bbox": [int(v) for v in line["bbox"]],
            "confidence": round(float(line["confidence"]), 4),
        }
        entry["needs_review"] = line_needs_review(entry, profile)
        lines.append(entry)

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

    page = {
        "page": int(page_number),
        "profile": profile,  # "FAST" | "HEAVY"
        "quality": {k: round(float(v), 4) for k, v in quality.items()},
        "lines": lines,
        "text": text,
        "needs_review": needs_review,
        "processing_ms": int(round(processing_ms)),
    }
    if suggestions is not None:
        page["suggestions"] = suggestions
    return page


def enrich_page(page: dict) -> dict:
    """Fill per-line needs_review on a stored page that predates the field.

    Idempotent: lines that already carry the flag keep it. Used by
    GET /jobs/{id} so jobs stored before per-line flags existed answer in
    the current shape without rewriting the DB."""
    profile = page.get("profile")
    lines = []
    for line in page.get("lines") or []:
        if "needs_review" in line:
            lines.append(line)
        else:
            lines.append({**line, "needs_review": line_needs_review(line, profile)})
    return {**page, "lines": lines}


def build_job_result(pages: list[dict]) -> dict[str, Any]:
    """Wrap per-page results into the final job result object."""
    return {"pages": pages}


def parse_job_result(result_json: str | None) -> Any:
    """DB -> JSON object (or None when the job isn't finished yet)."""
    if not result_json:
        return None
    import json

    return json.loads(result_json)
