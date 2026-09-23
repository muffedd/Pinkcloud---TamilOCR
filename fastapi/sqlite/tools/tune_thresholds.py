"""Tune FAST/HEAVY thresholds against a folder of sample scans (CICT).

Usage:
    python tools/tune_thresholds.py <folder-with-samples> [--json]

For every jpg/jpeg/png/tif/tiff/pdf in the folder it computes the 4
router metrics, prints a table, and suggests thresholds from the data:

  blur     -> keep the 25th percentile of the CLEANEST half (bad pages
              are blurry; the FAST badge must still admit clean scans)
  contrast -> 25th percentile of all samples
  noise    -> 75th percentile of all samples (grainy scans stay FAST
              unless really noisy)
  skew_deg -> max of clean samples + 1 degree of headroom

Copy the printed THRESHOLDS dict into app/router.py. Currently no CICT
sample scans are committed to this repo (licensing), so run this against
your local CICT sample folder.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pdfutil import load_pages, to_gray  # noqa: E402
from app.router import PageScores, compute_scores  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", type=Path, help="folder with sample scans")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    rows = []
    for p in sorted(args.folder.rglob("*")):
        if p.suffix.lower() not in {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".pdf"}:
            continue
        try:
            for i, page in enumerate(load_pages(p), start=1):
                s: PageScores = compute_scores(to_gray(page))
                rows.append({"file": f"{p.name}#p{i}", **vars(s)})
        except Exception as exc:
            print(f"WARN: {p.name}: {exc}", file=sys.stderr)

    if not rows:
        print("no readable samples found", file=sys.stderr)
        return

    def pct(values, q):
        v = sorted(values)
        return v[min(len(v) - 1, int(q * len(v)))]

    blur = [r["blur"] for r in rows]
    contrast = [r["contrast"] for r in rows]
    noise = [r["noise"] for r in rows]
    skew = [r["skew_deg"] for r in rows]

    suggested = {
        "blur": round(max(50.0, pct(blur, 0.25)), 1),
        "contrast": round(max(0.10, pct(contrast, 0.25)), 3),
        "noise": round(max(10.0, pct(noise, 0.75)), 1),
        "skew_deg": round(min(15.0, max(skew) + 1.0), 1),
    }

    if args.json:
        print(json.dumps({"samples": rows, "suggested_thresholds": suggested},
                         indent=2))
        return

    hdr = f"{'file':<28}{'blur':>10}{'contrast':>10}{'noise':>8}{'skew':>7}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['file']:<28}{r['blur']:>10.1f}{r['contrast']:>10.3f}"
              f"{r['noise']:>8.1f}{r['skew_deg']:>7.2f}")
    print("\nSuggested THRESHOLDS (paste into app/router.py):")
    print(f"THRESHOLDS = {json.dumps(suggested)}")


if __name__ == "__main__":
    main()