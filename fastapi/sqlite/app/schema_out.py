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
  "preprocessed": {...}, "corrections": [...], "verdicts": {...},
  "needs_review": bool, "processing_ms": int
}
"""

from __future__ import annotations

from typing import Any


def build_page_result(
    page_number: int,
    profile: str,
    quality: dict[str, float],
    ocr_lines: list[dict],
    processing_ms: float,
) -> dict[str, Any]:
    """Assemble the contract JSON for one page."""
    # Sort by reading order: top-to-bottom, then left-to-right within a band.
    ordered = sorted(
        ocr_lines,
        key=lambda l: (l["bbox"][1], l["bbox"][0]),
    )

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
        "needs_review": profile == "HEAVY",
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