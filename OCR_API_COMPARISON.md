# PaddleOCR vs Sarvam API comparison

> **Note (repo cleanup):** the raw scans now live at `samples/editor/sample1.png` and `sample2.png`. The repair outputs (`repair/out/`, `repair/verify/`), ground-truth text (`gt_*.txt`) and most `ocr_outputs/` artifacts referenced below were removed from the tree and are git-ignored; they are in git history (e.g. commit `3c53e7f`). Only `ocr_outputs/sarvam/raw/sample*/json/` and `.../metadata/page_001.json` remain, as test fixtures.

Both tested APIs: hosted **PaddleOCR-VL-1.6** and **Sarvam Document AI Digitise (Sarvam Vision 1.5, `ta-IN`)**. “Before” is the existing Paddle OCR output on the original raw sample. CER uses `gt_cict_narrinai_p3.txt` (sample1 only; sample2 is a different page).

Metrics: Tamil ratio and garbage rate are fractions; repeat-loop count is repeated-segment/word lines; confidence is the mean layout-box score per nonempty OCR line, not recognition certainty. Output line segmentation differs between services, so confidence/line counts are approximate cross-engine comparisons.

## Side-by-side results

| Page / original image | Before: Paddle on raw | Paddle API test | Sarvam API test | Latency (Paddle / Sarvam) |
|---|---|---|---|---|
| sample1 — [raw image](samples/editor/sample1.png) | CER **33.87%**; Tamil 0.845; garbage 0.060; loops 0; conf 0.717 | PaddleOCR-VL-1.6 on raw (same existing baseline): CER **33.87%**; Tamil 0.845; garbage 0.060; loops 0; conf 0.717 | Sarvam on raw: CER **1.54%** (22 edits / 1,429 GT chars); Tamil 0.978; garbage 0.000; loops 0; conf 0.742 | Paddle: not recorded; Sarvam: **12.54 s** |
| sample2 raw — [original image](samples/editor/sample2.png) | Existing Paddle “before” on raw: CER N/A; Tamil 0.920; garbage 0.055; loops 1; conf 0.808 | Same existing raw PaddleOCR-VL-1.6 result (not resubmitted); latency not recorded | Sarvam on the same raw image: CER N/A; Tamil 0.920; garbage 0.000; loops 0; conf 0.759 | Paddle: not recorded; Sarvam: **9.51 s** |
| sample2 repaired grayscale — [original image](samples/editor/sample2.png) | Raw baseline above; no matching GT | PaddleOCR-VL-1.6 on `_g.png`: CER N/A; Tamil 0.974; garbage 0.007; loops 0; conf 0.744 | Sarvam on `_g.png`: CER N/A; Tamil 0.986; garbage 0.000; loops 0; conf 0.798 | Paddle: **267.34 s POST; ~305.47 s to saved result**. Sarvam: **17.03 s** |

Sample1's OCR image is raw because the page gate classified it CLEAN. Sample2's OCR image is `repair/out/sample2_g.png` because the gate classified it DAMAGED. No binary image was submitted.

**Result:** Sarvam had much lower sample1 CER on the same raw image. On sample2 grayscale it also had higher Tamil ratio and lower garbage rate than Paddle, but CER cannot be assessed without matching ground truth. These results favor Sarvam on this small test set, not a general accuracy guarantee.

## Output text excerpts

| Page | Before / Paddle text | Sarvam text |
|---|---|---|
| sample1 | `21 - து, கீலவன் பிரியக்கருதிய தமிழக தோழர்...` | `தகவிசொல்லியது, எ - து, தலைவன் பிரியக்கருதிய...` |
| sample2, raw Paddle | `ஐவர கவஞஏஏ குரோல் பல்கலைக் கழகிக்கொண்டிருந்தார்கள்...` | `This image does not contain any legible text. It appears to be a blurry, abstract pattern or logo. பாரதம். ஐயா கவுசிகராஜனை...` |
| sample2, grayscale Paddle | `ஐவர கவஞஏஏ குரோல் பல்கலைக் கழகிக்கொண்டிருந்தார்கள்...` | `பாரதம். ஐயா கவுசிகராஜனை காதிராஜன் பெற்ற...` |

Excerpts are truncated; full output files are linked below. The Sarvam raw sample2 output starts with an incorrect English “no legible text” preamble before transcribing Tamil, so its low garbage-rate score does not capture that semantic error. OCR remains imperfect and needs human review.

## Latency notes

Latency is observed wall time for the API command through result retrieval/local save, based on recorded tool timestamps. For Paddle sample2, the POST itself took **267.34 s**; polling and fetching the completed result brought elapsed time from submission start to saved output to approximately **305.47 s**. Paddle sample1 latency was not captured. Sarvam sample1 and sample2 command wall times were **12.54 s** and **17.03 s**, respectively.

## Artifacts

| Engine / page | OCR text | JSON/layout metadata | Original downloaded result |
|---|---|---|---|
| Paddle sample1 raw | `ocr_outputs/raw/text/before_sample1.txt` | `ocr_outputs/raw/json/sample1.json` | — |
| Paddle sample2 raw | `ocr_outputs/raw/text/before_sample2.txt` | `ocr_outputs/raw/json/sample2.json` | — |
| Paddle sample2 grayscale | `ocr_outputs/repaired_gray/text/after_gray_sample2.txt` | `ocr_outputs/repaired_gray/json/sample2.json` | — |
| Sarvam sample1 raw | `ocr_outputs/sarvam/raw/sample1/text/after_sarvam_sample1.txt` | `ocr_outputs/sarvam/raw/sample1/json/sample1.json` | `ocr_outputs/sarvam/raw/sample1/raw/result.zip` |
| Sarvam sample2 raw | `ocr_outputs/sarvam/raw/sample2/text/after_sarvam_sample2.txt` | `ocr_outputs/sarvam/raw/sample2/json/sample2.json` | `ocr_outputs/sarvam/raw/sample2/raw/result.zip` |
| Sarvam sample2 grayscale | `ocr_outputs/sarvam/repaired_gray/sample2/text/after_sarvam_sample2.txt` | `ocr_outputs/sarvam/repaired_gray/sample2/json/sample2.json` | `ocr_outputs/sarvam/repaired_gray/sample2/raw/result.zip` |

No API key is included in this report.
