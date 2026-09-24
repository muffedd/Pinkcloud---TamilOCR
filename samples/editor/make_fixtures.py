#!/usr/bin/env python3
"""Build editor mock fixtures from committed Sarvam sample output.

Input : ocr_outputs/sarvam/raw/<sample>/<sample>.png/metadata/page_001.json
        (Sarvam Document AI blocks, bbox in page pixels) + samples/editor/<sample>.png (the scan)
Output: samples/editor/<sample>.json - one page in the schema/schema.json page
        shape, plus the proposed optional page-level `suggestions[]`
        (schema/suggestions-contract.md). Suggestions here are MOCK data.

Lines: each block's text is split on newlines and the block bbox is cut into
equal-height slices (the same approximation the Sarvam adapter uses).
Confidence: a stand-in for app/textcheck.py score_line() on the agreed scale
(clean Tamil 0.99, flawed 0.90 -> 0.62, low Tamil share -> 0, digits 0.3).
Swap in the real score_line once it lands on main; the editor only reads
line.confidence.

Usage: python3 samples/editor/make_fixtures.py
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.text_filter import orphan_signs  # noqa: E402

TAMIL = re.compile(r"[\u0b80-\u0bff]")
OK_PUNCT = set(" .,;:-!?'\"()[]—–")

# MOCK queue exercise: on this clean Sarvam text the proxy scores every
# sample1 line 0.99, so nothing would route. These lines are forced onto the
# flawed/garbage part of the scale (clearly fake) so the queue, crops and
# suggestions have something to show. Real confidence comes from score_line.
DEMO_FLAGS = {"sample1": {"L3": 0.90, "L8": 0.76, "L13": 0.62, "L20": 0.90, "L30": 0.54}}

SPECS = {
    "sample1": {"profile": "FAST", "needs_review": False},
    "sample2": {"profile": "HEAVY", "needs_review": True, "preprocessed": True},
}


def score_line(text):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.3 if any(c.isdigit() for c in text) else 0.0
    share = sum(bool(TAMIL.fullmatch(c)) for c in letters) / len(letters)
    if share < 0.5:
        return round(share * 0.6, 2)
    odd = sum(1 for c in text if not (TAMIL.fullmatch(c) or c.isdigit() or c in OK_PUNCT))
    flaws = len(orphan_signs(text)) + odd
    if flaws == 0 and share >= 0.9:
        return 0.99
    s = max(0.62, 0.90 - 0.07 * max(0, flaws - 1))
    if share < 0.9:
        s = min(s, 0.54)
    return round(s, 2)


def mock_candidates(word):
    """Plausible MOCK candidates: drop an orphan sign, trim stray marks."""
    out = []
    pos = orphan_signs(word)
    if pos:
        i = pos[0] - 1
        out.append(word[:i] + word[i + 1:])
    trimmed = word.strip(".,;:-—–!?")
    if trimmed and trimmed != word:
        out.append(trimmed)
    if len(word) > 3:
        out.append(word[:-1])
    seen, uniq = set(), []
    for c in out:
        if c and c != word and c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq[:3]


def build(sample):
    meta = json.loads((ROOT / f"ocr_outputs/sarvam/raw/{sample}/{sample}.png/metadata/page_001.json")
                      .read_text(encoding="utf-8"))
    lines = []
    for b in sorted(meta["blocks"], key=lambda b: b.get("reading_order", 0)):
        parts = [p for p in b["text"].split("\n") if p.strip()]
        if not parts:
            continue
        c = b["coordinates"]
        x, y, w, h = c["x1"], c["y1"], c["x2"] - c["x1"], c["y2"] - c["y1"]
        step = h / len(parts)
        for i, t in enumerate(parts):
            lines.append({"body": t.strip(), "bbox": [x, round(y + i * step), w, max(1, round(step))]})
    page = {"page": 1, "profile": SPECS[sample]["profile"], "quality": {
        "blur": 0.0, "contrast": 0.0, "noise": 0.0, "skew_deg": 0.0}, "lines": []}
    suggestions = []
    for seq, l in enumerate(lines, start=1):
        lid = f"L{seq}"
        conf = DEMO_FLAGS.get(sample, {}).get(lid, score_line(l["body"]))
        page["lines"].append({"id": lid, "seq": seq, "body": l["body"], "bbox": l["bbox"], "confidence": conf})
        if conf < 0.95:
            for wi, word in enumerate(l["body"].split(), start=1):
                cands = mock_candidates(word) if (orphan_signs(word) or wi <= 2) else []
                if cands and len(suggestions) < 24:
                    suggestions.append({"line": lid, "word": wi, "before": word, "candidates": [
                        {"text": t, "score": round(0.9 - 0.2 * k, 2), "source": "mock"}
                        for k, t in enumerate(cands)]})
    page["text"] = "\n".join(l["body"] for l in page["lines"])
    page["needs_review"] = SPECS[sample]["needs_review"]
    if SPECS[sample].get("preprocessed"):
        page["preprocessed"] = True
    page["suggestions"] = suggestions
    return page


if __name__ == "__main__":
    for s in SPECS:
        p = build(s)
        (ROOT / f"samples/editor/{s}.json").write_text(json.dumps(p, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        confs = [l["confidence"] for l in p["lines"]]
        print(s, len(p["lines"]), "lines", len(p["suggestions"]), "suggestion slots",
              "conf dist:", {k: sum(1 for c in confs if c == k) for k in sorted(set(confs))})
