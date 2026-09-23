#!/usr/bin/env python3
"""CER / WER for Tamil OCR output vs ground truth. Python 3 stdlib only.

Usage:
  python3 scripts/cer.py ground_truth.txt ocr_output.txt [--keep-unknown] [--no-punct] [--show-diff]

Both files are read as UTF-8, NFC-normalized, zero-width chars removed,
and all whitespace (spaces, tabs, newlines) collapsed to a single space.

CER = Levenshtein(ref, hyp) / len(ref), counted over Unicode code points
(e.g. "கொ" is 2 code points after NFC: க + ொ; "க்" is க + ்).
WER = word-level Levenshtein / number of reference words.

By default every "[?]" in the ground truth (unreadable span) is removed before
scoring, so the OCR is not penalized for text nobody could read. The OCR text
that corresponds to it will still count as insertions; that is a small,
conservative bias. Use --keep-unknown to score "[?]" literally.
"""
import argparse
import re
import sys
import unicodedata

ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)
PUNCT_RE = re.compile(r"[.,;:!?\"'()\[\]{}\-\u2013\u2014\u2018\u2019\u201c\u201d]")


def normalize(text, drop_unknown=True, no_punct=False):
    text = unicodedata.normalize("NFC", text).translate(ZERO_WIDTH)
    if drop_unknown:
        text = text.replace("[?]", "")
    if no_punct:
        text = PUNCT_RE.sub(" ", text)
    return " ".join(text.split())


def levenshtein(a, b):
    """Edit distance plus (sub, del, ins) counts. Works on str or list."""
    n, m = len(a), len(b)
    if n == 0:
        return m, (0, 0, m)
    if m == 0:
        return n, (0, n, 0)
    # full table so we can backtrace op counts; fine for page-sized text
    prev = list(range(m + 1))
    rows = [prev]
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        ai = a[i - 1]
        for j in range(1, m + 1):
            cost = 0 if ai == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        rows.append(cur)
        prev = cur
    i, j, s, d, ins = n, m, 0, 0, 0
    while i > 0 or j > 0:
        if i > 0 and j > 0 and rows[i][j] == rows[i - 1][j - 1] + (a[i - 1] != b[j - 1]):
            s += a[i - 1] != b[j - 1]
            i, j = i - 1, j - 1
        elif i > 0 and rows[i][j] == rows[i - 1][j] + 1:
            d += 1
            i -= 1
        else:
            ins += 1
            j -= 1
    return rows[n][m], (s, d, ins)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("ground_truth")
    p.add_argument("ocr_output")
    p.add_argument("--keep-unknown", action="store_true", help="score [?] markers literally")
    p.add_argument("--no-punct", action="store_true", help="ignore punctuation")
    p.add_argument("--show-diff", action="store_true", help="print a word-level diff")
    args = p.parse_args()

    with open(args.ground_truth, encoding="utf-8") as f:
        ref = normalize(f.read(), not args.keep_unknown, args.no_punct)
    with open(args.ocr_output, encoding="utf-8") as f:
        hyp = normalize(f.read(), not args.keep_unknown, args.no_punct)

    if not ref:
        sys.exit("ground truth is empty after normalization")

    dist, (s, d, i) = levenshtein(ref, hyp)
    wref, whyp = ref.split(), hyp.split()
    wdist, _ = levenshtein(wref, whyp)

    print(f"ref chars: {len(ref)}  hyp chars: {len(hyp)}")
    print(f"CER: {dist / len(ref):.2%}  (edits={dist}: sub={s} del={d} ins={i})")
    print(f"WER: {wdist / len(wref):.2%}  (word edits={wdist}, ref words={len(wref)})")

    if args.show_diff:
        import difflib
        for tok in difflib.ndiff(wref, whyp):
            if tok[0] in "+-":
                print(tok)


if __name__ == "__main__":
    main()
