# OCR accuracy workflow — research plan

**Goal:** raise Tamil OCR accuracy end to end. The FAST/HEAVY routing is scrapped;
Sarvam handles both. The new pipeline adds a document-type classifier, a damage
analyser, an image-candidate planner, a Tamil verifier with a semantic layer, and a
feedback loop that turns corrections into evaluation data.

Research method: primary sources only (model cards, configs, licenses, first-party
docs, and the repo's own GT+OCR data). No subagent tool is available in this harness,
so the legwork was done inline. Research date 2026-09-24.

## What already exists (reuse before building)

| Asset | What it gives us |
|---|---|
| `fastapi/sqlite/app/router.py` | `compute_scores()` → blur, contrast, noise, skew; `choose_profile()` |
| `repair/repair.py` | `background_correct`, `denoise`, `deskew_angle`, `sauvola`, `despeckle`, and `route()` (ink contrast + paper p90) |
| `fastapi/sqlite/app/ocr.py` | Gemini (FAST) + Sarvam Document AI `digitise` (HEAVY), returns `{body, bbox, confidence}` |
| `fastapi/sqlite/app/textcheck.py` | `score_line()` — a 0..1 text-quality proxy (Tamil share, orphan signs, odd chars, repeats) |
| `schema/suggestions-contract.md` | Proposed page-level `suggestions[]` with `{line, word, before, candidates:[{text,score,source}]}`, sources `lexicon\|repair\|rules\|llm` |
| `scripts/cer.py`, `scripts/text_filter.py` | CER/WER and the filter metrics |
| `ocr_outputs/`, `gt_cict_narrinai_p3.txt` | Sarvam + Paddle outputs and a ground-truth page for measurement |

The backend is CPU-only with a deliberately small dependency set
(`opencv-python-headless`, `numpy`, `pillow`, `pypdfium2`). Every stage below is
chosen to keep it that way.

## Pipeline

```
Upload
  → 1. document-type classifier      (vector classifier)
  → 2. damage / quality analyser
  → 3. image candidate planner
  → 4. Sarvam OCR
  → 5. Tamil verifier + semantic layer
  → 6. human correction
  → 7. evaluation data
```

---

## Stage 1 — Document-type classifier (vector classifier)

**Decision: embeddings/feature vectors + a linear classifier, not a fine-tuned CNN.**
Train offline, ship the weights, and infer with `numpy` so the runtime adds no ML
framework. The three classes are visually far apart (paper colour/texture, layout,
script style, leaf geometry), so this is a low-risk classifier.

**Feature/embedding options, cheapest first:**

| Option | Size | New runtime dep | Notes |
|---|---:|---|---|
| OpenCV feature vector: HSV/ Lab colour histograms + LBP texture + HOG + ink-density/row-profile stats at 128–256 px | 0 MB | **none** (`cv2`+`numpy` already present) | Start here. `cv2.HOGDescriptor`, `cv2.calcHist`; LBP is a few lines |
| `timm/tf_efficientnet_lite0.in1k` / `mobilenetv3_small_100` embeddings | 9–17 MB | `onnxruntime` (export to ONNX) | If handcrafted features fall short |
| `facebook/dinov2-small` | 84 MB | `onnxruntime`/`torch` | Strong general visual features |
| `timm/MobileCLIP2-S0-OpenCLIP` / `apple/MobileCLIP-S1-OpenCLIP` | small | `onnxruntime` | CLIP-style; also enables zero-shot prompts |
| `google/siglip2-base-patch16-224` | ~1.4 GB | `torch` | Overkill for this stage |

**Classifier:** linear SVM (`LinearSVC`) or logistic regression over the vectors, with
kNN as a no-training baseline. scikit-learn is the reference implementation for all
three; if we want zero new deps, train in scikit-learn offline and export `coef_`/
`intercept_` to JSON, then score with a numpy dot product at runtime (a linear model is
one line of inference). Add probability calibration only if the UI needs a confidence.

**Training data:** there is no public *modern / aged-manuscript / palm-leaf* dataset.
Build one:
- modern: `chainyo/rvl-cdip`, `maveriq/tobacco3482` (document-type corpora)
- manuscript/aged: `varunbhoyar/indic-historical-manuscripts`, `ved1245/synthetic-manuscript-dataset`
- palm leaf: scarce on HF — source from the CICT Tamil manuscript collection and
  augment with the repo's own `raw/sample2.png` (yellowed print) and palm-leaf images
- plus our own uploads, labelled once by a human

Keep a held-out split and report a confusion matrix; the classes are unbalanced in the
wild (most uploads are modern), so use class weights.

**Contract impact:** today `schema_out` emits `profile: FAST|HEAVY`. Add a `doc_type`
field (and keep `profile`) so the classifier result is visible and testable.

---

## Stage 2 — Damage / quality analyser

Extend `router.py`'s four metrics rather than replacing them. Cheap, CPU-only, and
each maps to a preprocessing decision:

| Signal | How | Drives |
|---|---|---|
| blur | Laplacian variance (exists) | denoise strength |
| contrast | Otsu ink-vs-paper (exists) | contrast restore |
| noise | median residual std (exists) | denoise on/off |
| skew | min-area-rect angle (exists) | deskew |
| yellowing | median (R−B) on bright pixels (`repair.choose_channel` already computes this) | channel choice / background normalisation |
| ink contrast | `repair.background_correct` returns it; `route()` uses it | faded-page gate |
| background whiteness | input p90 (`repair.route`) | is the paper already white |
| stain/foxing | local std of the background estimate; large dark blobs on paper | background normalisation |
| fading | ratio of light-ink pixels | Sauvola `k` |
| tears/warping | optional; only if it changes OCR (measure first) | dewarp later, YAGNI |

Rule of thumb: **only add a metric when it changes a decision.** Each metric must be
justified by a measurable CER delta on the sample pages, or it is dead weight.

---

## Stage 3 — Image candidate planner

Generate candidate images, then choose which to send. Candidates come from code that
already exists in `repair/repair.py`:

1. `original` — untouched page
2. `bg_normalised_gray` — `background_correct()` (divide out paper, stretch)
3. `deskewed_contrast` — `background_correct` + `denoise` + `deskew_angle` + `rotate`
4. `binary` — `sauvola()` (Sauvola is the documented local-threshold method; Otsu is the
   global baseline). **Only when beneficial** — binarisation can erase thin strokes and
   pulli dots, which is exactly the failure mode `repair.py` guards against.

**Selection strategies, cheapest first:**
1. **Heuristic gate (start here):** `repair.route()` already decides CLEAN vs DAMAGED
   from ink contrast + paper p90. CLEAN → `original`; DAMAGED → `deskewed_contrast`;
   binary only when the contrast is extreme. Zero extra OCR calls.
2. **Two-candidate vote (next rung):** send `original` and `deskewed_contrast`, score
   each with `textcheck.score_line()` (or Sarvam block confidence), keep the higher.
   Costs 2× OCR; measure whether the accuracy gain is worth it before enabling.
3. **Learned policy (last):** predict the best candidate from the Stage-2 metrics.
   Only after (2) gives labelled outcomes.

Historical-document binarisation has a benchmark lineage (DIBCO / H-DIBCO, ICDAR) if we
ever need to tune the threshold; for now Sauvola + the existing gate is enough.

---

## Stage 4 — Sarvam OCR

Already integrated (`ocr.py`). Two facts to design around:
- Sarvam returns **layout blocks**, not lines; `ocr.py` splits block text on newlines
  and divides the block box evenly, so line bboxes are an approximation.
- Its `confidence` is a **layout score, not recognition certainty**, which is why
  `textcheck.score_line()` exists as the per-line proxy.

Keep sending one candidate image per page in the first version. The verifier (Stage 5)
is where accuracy is actually recovered.

---

## Stage 5 — Tamil verifier + semantic layer

This is the main accuracy lever. Layer the checks cheapest-first; each layer can veto
or re-rank, and every suggestion carries a `source` (`lexicon|repair|rules|llm`).

### 5a. Deterministic rules (no model)
- `textcheck.score_line()` already flags low Tamil share, orphan vowel signs/virama,
  odd characters, and repetition loops.
- Reuse `scripts/text_filter.py` for dedupe/drop.

### 5b. Confusable substitution (data-driven, not guessed)
Generate candidates by swapping characters that **our own OCR actually confuses**. From
`gt_cict_narrinai_p3.txt` vs the recorded outputs:

| Engine | Top real substitutions |
|---|---|
| Sarvam (low CER) | ந→த ×2, ண→ள, ற→க, ன→ண, ொ→ோ, ொ→ா, ீ→ி |
| Paddle (high CER) | த→க ×9, ந→க ×6, ன→ண ×5, ந→த ×4, ை→வ ×4, ை→ல ×3, ல→ி ×3 |

These pairs seed a substitution table; score each variant against a lexicon/LM and rank.
The table is regenerated as more corrected data lands, so it improves on its own.

### 5c. Lexicon + morphology
- **Lexicon:** Project Madurai (public-domain Tamil texts) plus the CICT classical-Tamil
  collection; build a word-frequency list. A word absent from the lexicon is a
  candidate for review, and near-lexicon words become candidates.
- **Morphology:** `apertium-tam` (GPL-3.0 FST), `tacola-aucse/Morphological-Analyzer-For-Tamil`,
  and `akilan-2022/Morphological-Analysis-for-Classical-Tamil` (classical Tamil, matches
  our Narrinai-style GT). Morphology lets us accept valid inflected forms a flat
  lexicon would reject, and generate morphologically plausible candidates.
- **Spell checkers as references:** `Tamil-Virtual-Academy/Tamilinaiya-Spellchecker`
  (GPL-2.0) and `tacola-auceg/spellchecker_ta` ("Annam").

### 5d. Language-model scoring
Score candidate sentences with a Tamil LM to pick the most fluent: `l3cube-pune/tamil-bert`
or `ai4bharat/IndicBERTv2-MLM-only` (pseudo-log-likelihood), or a Tamil LLM for a
slower, stronger re-rank. Use this to rank candidates, never to silently rewrite text.

### 5e. Semantic layer (embeddings + retrieval)
Purpose: use **meaning and corpus memory** to correct and validate, not just spelling.

- **Embed** each OCR line (and each candidate) with a Tamil/multilingual sentence encoder.
  Lightweight, ONNX-friendly options: `Xenova/multilingual-e5-small` (small, strong
  multilingual), `l3cube-pune/tamil-sentence-similarity-sbert` (Tamil-specific),
  `google/embeddinggemma-300m` (300M, has ONNX + q4 GGUF). Heavier: `BAAI/bge-m3`,
  `sentence-transformers/LaBSE`.
- **Store** the corpus vectors in **`sqlite-vec`** (Apache-2.0) — the app already uses
  SQLite, so this adds no server and no new datastore. Brute-force numpy is fine for a
  few thousand lines if even that is too much.
- **Retrieve** the nearest known lines/verses. If an OCR line matches a canonical text
  at high similarity, offer the canonical wording as the top candidate (`source:
  lexicon`/`llm`). This is how classical Tamil (Project Madurai, CICT) gets corrected
  exactly rather than approximately.
- **Flag** lines whose best corpus similarity is low — a semantic anomaly signal that
  complements `score_line()`'s character-level one.
- **Word-level neighbours:** nearest words in embedding space are semantic candidates
  when morphology alone is ambiguous.

Wire the output into the existing `suggestions[]` contract; no schema change is needed
for the array itself (it is already specified), only the schema fragment must be added
in the same change that starts emitting it.

---

## Stage 6 — Human correction

Already built (editor, doubt queue, J/K/Enter). Two changes for this plan:
- **Candidate-aware queue:** show `suggestions[]` on the doubtful words (the contract
  already reserves number keys 1–9 for this).
- **Calibration:** the queue is driven by confidence. `textcheck.score_line()` is a
  heuristic proxy; once corrections exist, calibrate it (e.g. reliability curve / ECE)
  so the queue surfaces the words most likely to be wrong.

---

## Stage 7 — Evaluation data

Every human correction is a labelled example. Store it and close the loop:
- **Record** `(page image crop, OCR text, corrected text, doc_type, damage metrics,
  candidate_used)` per accepted fix.
- **Measure** with `scripts/cer.py` (CER/WER) against `gt_cict_narrinai_p3.txt` and any
  new GT pages; track Tamil ratio / garbage rate with `scripts/text_filter.py`.
- **Benchmark datasets** for regression: `meharuhanzz/OCR-Bench1000-Tamil`,
  `mvbalaji/tamil-ocr-benchmark`, `Nevidu/tamil_synthetic_ocr`.
- **Active learning:** the corrected pairs retrain (a) the confusable table, (b) the
  lexicon weights, and (c) optionally the candidate ranker. Because Stage 5b is
  data-driven, the system gets more accurate with use and no manual model surgery.
- **Guardrails:** keep a frozen eval set so "improvements" that only fit the latest
  pages are caught.

---

## Contract and schema changes

| Change | Where |
|---|---|
| `doc_type` field (modern/aged_manuscript/palm_leaf) | `schema/schema.json`, `schema_out.py` |
| `suggestions[]` array actually emitted | `schema/schema.json` (fragment already drafted in `suggestions-contract.md`) |
| optional `variants[]` / chosen-candidate record | `schema/schema.json` if we keep Stage-3 history |
| correction records for training | `db.py` (new table or JSON column) |

All page objects are `additionalProperties: false`, so each new field must be added to
the schema in the same change that emits it.

## Build order (lazy, each rung ships alone)

1. **Doc-type classifier** — OpenCV feature vectors + linear SVM; add `doc_type` to the contract.
2. **Damage metrics** — extend `router.py`; keep only metrics that move CER.
3. **Candidate planner** — heuristic gate first; two-candidate vote behind a flag.
4. **Verifier** — lexicon + confusables (from our data) + `score_line()`; emit `suggestions[]`.
5. **Semantic layer** — embeddings + `sqlite-vec` over Project Madurai/CICT.
6. **Eval loop** — record corrections, track CER, retrain confusables/lexicon.

Each stage is independently measurable against `gt_cict_narrinai_p3.txt`, so we can
stop at any rung and still ship an improvement.

## Open questions / risks

- **Palm-leaf data is scarce**; the classifier may start as modern-vs-aged only and add
  palm leaf when enough labelled images exist.
- **Sarvam line bboxes are approximations**, so box-level correction UI is only as good
  as the block split.
- **Licenses:** `apertium-tam` GPL-3.0, Tamilinaiya GPL-2.0, `sqlite-vec` Apache-2.0,
  most HF encoders Apache-2.0/MIT — check each before bundling into the app.
- **New dependencies:** the classifier can be numpy-only; the semantic layer needs one
  ONNX encoder + `sqlite-vec`. Keep both optional so the core backend still boots without
  them (the core backend already boots with no local model installed).

## Sources

- Doc-type datasets: `chainyo/rvl-cdip`, `maheriq/tobacco3482` → https://huggingface.co/datasets ; historical: https://huggingface.co/datasets/varunbhoyar/indic-historical-manuscripts
- Embedding models: https://huggingface.co/facebook/dinov2-small, https://huggingface.co/timm/MobileCLIP2-S0-OpenCLIP, https://huggingface.co/google/siglip2-base-patch16-224, https://huggingface.co/timm/tf_efficientnet_lite0.in1k
- Classifiers: scikit-learn `LinearSVC` / `LogisticRegression` / `KNeighborsClassifier` — https://scikit-learn.org/stable/modules/svm.html
- Palm-leaf OCR: https://github.com/back-kh/SADA-Ancient-Palm-Leaf-Manuscripts-Recognitions (APSIPA 2022, PRL 2025); https://github.com/TamilPalmLeafManuscriptCharacters/Tamil-Palm-Leaf-Manuscript-Characters
- Binarisation benchmarks: DIBCO / H-DIBCO (ICDAR) — https://github.com/beargolden/H-DIBCO-2018
- Sentence encoders: https://huggingface.co/Xenova/multilingual-e5-small, https://huggingface.co/l3cube-pune/tamil-sentence-similarity-sbert, https://huggingface.co/google/embeddinggemma-300m, https://huggingface.co/BAAI/bge-m3
- Vector store: https://github.com/asg017/sqlite-vec (Apache-2.0)
- Tamil NLP: https://github.com/apertium/apertium-tam (GPL-3.0); https://github.com/tacola-aucse/Morphological-Analyzer-For-Tamil; https://github.com/akilan-2022/Morphological-Analysis-for-Classical-Tamil; https://github.com/Tamil-Virtual-Academy/Tamilinaiya-Spellchecker (GPL-2.0)
- Language models: https://huggingface.co/l3cube-pune/tamil-bert, https://huggingface.co/ai4bharat/IndicBERTv2-MLM-only
- Tamil corpora: Project Madurai https://www.projectmadurai.org/ ; CICT classical Tamil collection
- Tamil OCR eval sets: https://huggingface.co/datasets/meharuhanzz/OCR-Bench1000-Tamil, https://huggingface.co/datasets/mvbalaji/tamil-ocr-benchmark, https://huggingface.co/datasets/Nevidu/tamil_synthetic_ocr
