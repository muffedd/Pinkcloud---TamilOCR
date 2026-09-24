# OCR test scores

> **Note (repo cleanup):** the raw scans now live at `samples/editor/sample1.png` and `sample2.png`. The repair outputs (`repair/out/`, `repair/verify/`), ground-truth text (`gt_*.txt`) and most `ocr_outputs/` artifacts referenced below were removed from the tree and are git-ignored; they are in git history (e.g. commit `3c53e7f`). Only `ocr_outputs/sarvam/raw/sample*/json/` and `.../metadata/page_001.json` remain, as test fixtures.

## OCR comparison

`tamil_ratio` and `garbage_rate` are fractions. `mean_conf` is the mean layout-box score (not recognition certainty). `repeat_loop_lines` counts lines with a 5× repeated segment/word. CER is NFC-normalized character edit distance against `gt_cict_narrinai_p3.txt`; sample2 is a different page, so CER is unavailable.

| Page | Input | tamil_ratio | garbage_rate | repeat_loop_lines | mean_conf | CER |
|---|---|---:|---:|---:|---:|---:|
| sample1 | Raw | 0.845 | 0.060 | 0 | 0.717 | 33.87% |
| sample1 | Raw + filter | 0.979 | 0.001 | 0 | 0.712 | **27.22%** |
| sample1 | Repaired grayscale | 0.834 | 0.064 | 0 | 0.749 | 40.10% |
| sample1 | Repaired grayscale + filter | 0.977 | 0.000 | 0 | 0.753 | 31.91% |
| sample2 | Raw | 0.920 | 0.055 | 1 | 0.808 | N/A |
| sample2 | Raw + filter | 0.974 | 0.000 | 1 | 0.941 | N/A |
| sample2 | Repaired grayscale | 0.974 | 0.007 | 0 | 0.744 | N/A |
| sample2 | Repaired grayscale + filter | 0.983 | 0.001 | 0 | 0.938 | N/A |

## Filter results

| Page/input | Lines dropped (<50% Tamil) | Exact repeats removed | Lines kept |
|---|---:|---:|---:|
| sample1 raw | 2 | 0 | 7 |
| sample1 repaired grayscale | 2 | 0 | 18 |
| sample2 raw | 4 | 0 | 4 |
| sample2 repaired grayscale | 3 | 0 | 3 |

## Per-page gate

Thresholds: ink contrast ≥ 0.25 and input background p90 ≥ 240.

| Page | Ink contrast | Background p90 | Decision | OCR image selected |
|---|---:|---:|---|---|
| sample1 | 0.773 | 255.0 | CLEAN | Raw image |
| sample2 | 0.518 | 138.0 | DAMAGED | Repaired grayscale (`_g.png`) |

## Checks and outcome

- Gate self-check: **PASS**; text-filter checks: **PASS**; Python compile check: **PASS**.
- Hosted grayscale OCR for sample2 completed; submission took **267.34 s**. No binary image was submitted.
- Sample1 CER winner: **raw + filter, 27.22%**; gate chose the raw path, matching the measured winner.
- Sample2: no matching ground truth, so no CER winner can be declared. Filtered grayscale had 0 repeat-loop lines versus 1 for filtered raw, but retained 1,776 characters versus 3,208; evidence is inconclusive.

## Sarvam Document AI trial

Used Sarvam Document AI Digitise (`ta-IN`, Markdown output) on sample1 raw and sample2 repaired grayscale. Mean confidence is the mean layout-block score repeated across that block's nonempty OCR lines; line segmentation differs across engines, so compare cautiously.

| Page/input | Engine | tamil_ratio | garbage_rate | repeat_loop_lines | mean_conf | CER |
|---|---|---:|---:|---:|---:|---:|
| sample1 raw | PaddleOCR-VL-1.6 | 0.845 | 0.060 | 0 | 0.717 | 33.87% |
| sample1 raw | Sarvam Document AI | 0.978 | 0.000 | 0 | 0.742 | **1.54%** |
| sample2 repaired grayscale | PaddleOCR-VL-1.6 | 0.974 | 0.007 | 0 | 0.744 | N/A |
| sample2 repaired grayscale | Sarvam Document AI | 0.986 | 0.000 | 0 | 0.798 | N/A |

Sarvam sample1 CER: 22 edits / 1,429 normalized ground-truth characters. Sample2 has no matching ground truth. This is a successful OCR API trial, not yet a change to the application's OCR backend.

## Output locations

- Raw: `ocr_outputs/raw/`
- Repaired: `ocr_outputs/repaired/`
- Repaired grayscale: `ocr_outputs/repaired_gray/`
