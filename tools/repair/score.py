#!/usr/bin/env python3
"""Compare OCR output BEFORE vs AFTER repair. Reads .txt or .json (any shape:
collects every string under keys like text/rec_text/content/block_content and
every number under confidence/score/rec_score). No schema assumptions.

usage: python3 score.py before.(json|txt) after.(json|txt)
"""
import json, re, statistics, sys

TEXT_KEYS = {"text", "rec_text", "rec_texts", "content", "block_content", "line", "transcription"}
CONF_KEYS = {"confidence", "conf", "score", "rec_score", "rec_scores"}
TAMIL = re.compile(r"[\u0B80-\u0BFF]")
CONS = set(range(0x0B95, 0x0BBA))
MARKS = set(range(0x0BBE, 0x0BCE)) | {0x0B82, 0x0BD7}
OK = re.compile(r"[\u0B80-\u0BFF0-9A-Za-z\s.,;:!?'\"()\-\u2013\u2014/]")

def walk(o, texts, confs, key=None):
    if isinstance(o, dict):
        for k, v in o.items(): walk(v, texts, confs, k.lower())
    elif isinstance(o, list):
        for v in o: walk(v, texts, confs, key)
    elif isinstance(o, str) and key in TEXT_KEYS:
        texts.append(o)
    elif isinstance(o, (int, float)) and not isinstance(o, bool) and key in CONF_KEYS:
        confs.append(float(o))

def load(p):
    raw = open(p, encoding="utf-8").read()
    texts, confs = [], []
    try:
        walk(json.loads(raw), texts, confs)
    except json.JSONDecodeError:
        texts = [raw]
    lines = [l for t in texts for l in t.splitlines() if l.strip()]
    return lines, confs

def metrics(lines, confs):
    s = "".join(lines).replace(" ", "")
    n = max(len(s), 1)
    orphan = 0
    for i, ch in enumerate(s):
        if ord(ch) in MARKS and (i == 0 or ord(s[i - 1]) not in CONS | MARKS):
            orphan += 1
    loops = sum(1 for l in lines if re.search(r"(.{2,6})\1{4,}", l))
    return {
        "lines": len(lines),
        "chars": len(s),
        "tamil_ratio": round(len(TAMIL.findall(s)) / n, 3),
        "garbage_rate": round(sum(1 for c in s if not OK.match(c)) / n, 3),
        "orphan_mark_rate": round(orphan / n, 4),
        "repeat_loop_lines": loops,
        "mean_conf": round(statistics.mean(confs), 3) if confs else None,
        "low_conf_lines(<0.8)": sum(1 for c in confs if c < 0.8) if confs else None,
    }

b, a = metrics(*load(sys.argv[1])), metrics(*load(sys.argv[2]))
print(f"{'metric':22}{'before':>10}{'after':>10}")
for k in b: print(f"{k:22}{str(b[k]):>10}{str(a[k]):>10}")
win = 0; lose = 0
def cmp(x, y, higher_better):
    global win, lose
    if x is None or y is None or x == y: return
    if (y > x) == higher_better: win += 1
    else: lose += 1
cmp(b["tamil_ratio"], a["tamil_ratio"], True)
cmp(b["garbage_rate"], a["garbage_rate"], False)
cmp(b["orphan_mark_rate"], a["orphan_mark_rate"], False)
cmp(b["repeat_loop_lines"], a["repeat_loop_lines"], False)
cmp(b["mean_conf"], a["mean_conf"], True)
drop = b["lines"] and a["lines"] < 0.85 * b["lines"]
verdict = "FAIL (lost lines)" if drop else ("PASS" if win > lose else "FAIL" if lose > win else "NEUTRAL")
print(f"\nVERDICT: {verdict}   (better on {win}, worse on {lose})")
