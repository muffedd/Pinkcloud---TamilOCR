# OCR repair progress and evidence

## Source and generated image samples

| Page | Raw input | Repair output (grayscale; OCR-safe) | Binary output (display only) |
|---|---|---|---|
| sample1 | `raw/sample1.png` | `repair/out/sample1_g.png` | `repair/out/sample1.png` |
| sample2 | `raw/sample2.png` | `repair/out/sample2_g.png` | `repair/out/sample2.png` |

**Safety:** The binary files are for display only and were not submitted to OCR. Raw pages were submitted as raw images; repaired pages used grayscale `_g.png` images.

## Hosted OCR submission

- Model: PaddleOCR-VL-1.6
- Endpoint: `https://paddleocr.aistudio-app.com/api/v2/ocr/jobs`
- Input: `repair/out/sample2_g.png`
- Credential: supplied via `PADDLE_TOKEN`; intentionally not recorded here.
- The one requested resubmission completed: POST took **267.34 s**, and the job reached `done` on the second poll.
- Result JSON: `ocr_outputs/repaired_gray/json/sample2.json`
- Flattened OCR text: `ocr_outputs/repaired_gray/text/after_gray_sample2.txt`

## OCR comparison scores

Metrics: Tamil-character share and garbage share are fractions; confidence is the mean layout-box score (not recognition certainty). CER is normalized character edit distance against `gt_cict_narrinai_p3.txt`. That ground truth matches sample1 only. Repeat-loop count uses the reported repeated-segment/word check.

| Page | OCR path | tamil_ratio | garbage_rate | repeat_loop_lines | mean_conf | CER |
|---|---|---:|---:|---:|---:|---:|
| sample1 | Raw | 0.845 | 0.060 | 0 | 0.717 | 33.87% |
| sample1 | Raw + filter | 0.979 | 0.001 | 0 | 0.712 | **27.22%** |
| sample1 | Repaired grayscale | 0.834 | 0.064 | 0 | 0.749 | 40.10% |
| sample1 | Repaired grayscale + filter | 0.977 | 0.000 | 0 | 0.753 | 31.91% |
| sample2 | Raw | 0.920 | 0.055 | 1 | 0.808 | N/A |
| sample2 | Raw + filter | 0.974 | 0.000 | 1 | 0.941 | N/A |
| sample2 | Repaired grayscale | 0.974 | 0.007 | 0 | 0.744 | N/A |
| sample2 | Repaired grayscale + filter | 0.983 | 0.001 | 0 | 0.938 | N/A |

Sample1 winner by CER: **raw + filter**. Sample2 has no matching ground truth, so no CER winner can be established. Sample2 filtered grayscale has no detected repeat-loop line versus one in filtered raw, but is not enough to conclude it is more accurate.

## Sarvam Document AI alternative trial

Sarvam's documented Document AI Digitise API (Sarvam Vision 1.5) supports Tamil with `language=ta-IN`; endpoint: `POST https://api.sarvam.ai/doc-ai/v1/job/digitise`. Both jobs completed. The key was read from ignored `.env` and is not recorded here.

| Test | Input | Result |
|---|---|---|
| sample1 | `raw/sample1.png` | CER **1.54%** (22 edits / 1,429 normalized GT chars); tamil_ratio 0.978, garbage_rate 0.000, repeat_loop_lines 0, mean_conf 0.742 |
| sample2 | `repair/out/sample2_g.png` | CER N/A (no matching GT); tamil_ratio 0.986, garbage_rate 0.000, repeat_loop_lines 0, mean_conf 0.798 |

For sample1, PaddleOCR-VL-1.6 raw measured CER 33.87% (Sarvam: 1.54%) on the same raw image. Sample2 Paddle grayscale had tamil_ratio 0.974 / garbage_rate 0.007; Sarvam grayscale had 0.986 / 0.000. Confidence is averaged per nonempty output line using its layout-block score; segmentation differs between services. **Sarvam is currently a tested alternative, not wired into the application backend.**

Artifacts:
- sample1 text/Markdown: `ocr_outputs/sarvam/raw/sample1/text/after_sarvam_sample1.txt` and `.md`
- sample1 page JSON: `ocr_outputs/sarvam/raw/sample1/json/sample1.json`
- sample2 text/Markdown: `ocr_outputs/sarvam/repaired_gray/sample2/text/after_sarvam_sample2.txt` and `.md`
- sample2 page JSON: `ocr_outputs/sarvam/repaired_gray/sample2/json/sample2.json`
- Original downloaded result archives and manifests are under each sample's `raw/` folder.

## Filter results

| Page/path | Lines dropped (<50% Tamil) | Exact duplicate sentences removed | Lines kept |
|---|---:|---:|---:|
| sample1 raw | 2 | 0 | 7 |
| sample1 repaired grayscale | 2 | 0 | 18 |
| sample2 raw | 4 | 0 | 4 |
| sample2 repaired grayscale | 3 | 0 | 3 |

Filtering drops/deduplicates/flags OCR text; it does not correct or rewrite retained text. Per-line details are in the `reports/` directories below.

## Per-page routing gate

The gate uses the existing `CONTRAST_FADED=0.25` threshold and a near-white paper threshold of input p90 ≥ 240.

| Page | Ink contrast | Background p90 | Route | Chosen OCR input |
|---|---:|---:|---|---|
| sample1 | 0.773 | 255.0 | CLEAN | `raw/sample1.png` |
| sample2 | 0.518 | 138.0 | DAMAGED | `repair/out/sample2_g.png` |

The gate logs each decision and metric when running `repair/repair.py`. Sample1's clean route agrees with the raw-path CER winner.

## Runnable checks

Commands run and successful:

```sh
python3 test_text_filter.py
# text_filter checks passed

~/ocrfix/bin/python test_repair_gate.py
# gate checks passed

python3 -m py_compile repair/repair.py
# passed (no output)

~/ocrfix/bin/python repair/repair.py raw repair/out --threads 2
# sample1: CLEAN ... OCR=raw/sample1.png
# sample2: DAMAGED ... OCR=repair/out/sample2_g.png
```

The repair run reported sample2: yellowed=True, gap=0.842, k=0.15, win=49, skew=0.00, up=True, 0.15 s total. The gate's input ink contrast metric is distinct from the `gap` measured after background correction.

## Artifact index

All OCR results were relocated into these categories:

- Raw: `ocr_outputs/raw/{json,text,filtered,reports}/`
- Existing repaired OCR outputs: `ocr_outputs/repaired/{json,text,filtered,reports}/`
- Repaired-grayscale OCR: `ocr_outputs/repaired_gray/{json,text,filtered,reports}/`
- Filtered files end in `_clean.txt`; reports end in `_report.txt`.
- Comparison summary: `OCR_TEST_SCORES.md`

The legacy repaired folder has a sample2 filtered file but no sample1 filtered artifact; the four-row comparison above uses raw and repaired-grayscale outputs as requested.
