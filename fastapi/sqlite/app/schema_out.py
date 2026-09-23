"""Output contract builder for Pink Cloud.

Frozen shape (do not add required fields, do not rename):

{
  "page": int,
  "profile": "FAST" | "HEAVY",
  "quality": {"blur": f, "contrast": f, "noise": f, "skew_deg": f},
  "lines": [
     {"id": "L1", "seq": 1, "body": str,
      "bbox": [x, y, w, h], "confidence": 0..1}
  ],
  "text": "<line bodies stitched in seq order>",
  # optional keys below
  "preprocessed": bool, "corrections": [...], "verdicts": [...],
  "needs_review": bool, "processing_ms": int
}
"""

from __future__ import annotations

from typing import Any

# A page goes to the review queue when any line is below this confidence
# (matches the editor's "auto" band), when it has no lines at all, or when
# it was routed HEAVY (no repair pass exists yet). Stub lines are 0.0.
REVIEW_CONFIDENCE = 0.85


def _reading_order(ocr_lines: list[dict]) -> list[dict]:
    """Top-to-bottom by visual line band, then left-to-right in each band.

    Boxes whose vertical centres are within half a line height of a band
    belong to it, so a right-hand box a few px higher than the left one
    (skew, jitter) no longer sorts first.
    """
    items = sorted(
        ocr_lines,
        key=lambda l: (l["bbox"][1] + l["bbox"][3] / 2.0, l["bbox"][0]),
    )
    bands: list[dict] = []
    for line in items:
        _x, y, _w, h = line["bbox"]
        cy, h = y + h / 2.0, max(float(h), 1.0)
        band = bands[-1] if bands else None
        if band is not None and abs(cy - band["cy"]) <= 0.5 * min(h, band["h"]):
            band["lines"].append(line)
            n = len(band["lines"])
            band["cy"] += (cy - band["cy"]) / n
            band["h"] += (h - band["h"]) / n
        else:
            bands.append({"cy": cy, "h": h, "lines": [line]})
    ordered: list[dict] = []
    for band in bands:
        ordered.extend(sorted(band["lines"], key=lambda l: l["bbox"][0]))
    return ordered


def needs_review(profile: str, lines: list[dict]) -> bool:
    """Low confidence (or no text, or an unrepaired HEAVY page) -> review."""
    if not lines or profile == "HEAVY":
        return True
    return min(float(l["confidence"]) for l in lines) < REVIEW_CONFIDENCE


def build_page_result(
    page_number: int,
    profile: str,
    quality: dict[str, float],
    ocr_lines: list[dict],
    processing_ms: float,
) -> dict[str, Any]:
    """Assemble the contract JSON for one page."""
    # Reading order: top-to-bottom by line band, left-to-right within a band.
    ordered = _reading_order(ocr_lines)

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

    return {
        "page": int(page_number),
        "profile": profile,  # "FAST" | "HEAVY"
        "quality": {k: round(float(v), 4) for k, v in quality.items()},
        "lines": lines,
        "text": text,
        # Optional extras (allowed by the contract, useful for the demo):
        "needs_review": needs_review(profile, lines),
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