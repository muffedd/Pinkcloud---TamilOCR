#!/usr/bin/env python3
"""Drop non-Tamil OCR lines, dedupe exact sentences and report confidence/marks.

Usage: python3 text_filter.py after_gray_sample1.txt --json ocr_after_gray/sample1.json
Confidence is the matching layout box's score (block-level, not text recognition).
"""
import argparse
import json
import re
from pathlib import Path

TAMIL = re.compile(r"[\u0b80-\u0bff]")
# Vowel signs only; exclude anusvara and virama. A preceding vowel sign can
# follow a consonant in decomposed Tamil forms (e.g. கொ).
SIGNS = set(range(0x0bbe, 0x0bcd)) | {0x0bd7}
CONS = set(range(0x0b95, 0x0bba))
SENTENCES = re.compile(r"[^.!?\u0964\u0965]+[.!?\u0964\u0965]*")


def blocks(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    result = data["result"]["layoutParsingResults"][0]["prunedResult"]
    boxes = {tuple(box["coordinate"]): box["score"] for box in result["layout_det_res"]["boxes"]}
    # Same ordering as the existing before_sampleN.txt / after_sampleN.txt flatten.
    ordered = sorted(result["parsing_res_list"], key=lambda b: (
        b.get("block_order") or 0, b.get("block_bbox", [0, 0])[1]))
    for block in ordered:
        yield (block.get("block_content") or "").splitlines(), boxes.get(tuple(block["block_bbox"]))


def orphan_signs(line):
    return [i + 1 for i, ch in enumerate(line) if ord(ch) in SIGNS and
            (i == 0 or ord(line[i - 1]) not in CONS | SIGNS)]


def clean(lines, confidences):
    output, notes, seen = [], [], set()
    dropped = repeats = 0
    for number, (line, confidence) in enumerate(zip(lines, confidences, strict=True), 1):
        if not line.strip():
            continue
        # Share is among script letters, not whitespace/digits/punctuation.
        letters = [c for c in line if c.isalpha()]
        share = sum(bool(TAMIL.fullmatch(c)) for c in letters) / max(len(letters), 1)
        if share < .5:
            dropped += 1
            continue
        kept = []
        for match in SENTENCES.finditer(line):
            sentence = match.group()
            key = sentence.strip()
            if key and key in seen:
                repeats += 1
            else:
                kept.append(sentence)
                if key:
                    seen.add(key)
        text = "".join(kept)
        if not text.strip():
            continue
        marks = orphan_signs(text)
        route = "REVIEW" if confidence is None or confidence < .5 else "FLAG" if confidence < .8 else "KEEP"
        notes.append((number, route, confidence, marks))
        output.append(text)
    return output, dropped, repeats, notes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", type=Path)
    parser.add_argument("--json", type=Path, help="original OCR JSON for block confidence")
    args = parser.parse_args()
    source = args.text.read_text(encoding="utf-8").splitlines()
    actual = [line for line in source if line.strip()]
    if args.json:
        block_data = list(blocks(args.json))
        expected = [line for block, _ in block_data for line in block if line.strip()]
        if actual != expected:
            parser.error("text does not match JSON block contents/order; cannot safely map confidence")
        confs = [score for block, score in block_data for line in block if line.strip()]
    else:
        confs = [None] * len(actual)  # no invented confidence; route to REVIEW
    cleaned, dropped, repeats, notes = clean(actual, confs)
    base = args.text.with_suffix("")
    base.with_name(base.name + "_clean.txt").write_text("\n".join(cleaned) + "\n", encoding="utf-8")
    report = [f"lines dropped (<50% Tamil letters): {dropped}",
              f"exact repeated sentences removed: {repeats}",
              f"lines kept: {len(cleaned)}", "confidence: layout-box score, not OCR certainty"]
    report += [f"source line {n}: {route} (confidence={score if score is not None else 'missing'}, orphan sign columns={marks})"
               for n, route, score, marks in notes]
    base.with_name(base.name + "_report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report[:4]))


if __name__ == "__main__":
    main()
