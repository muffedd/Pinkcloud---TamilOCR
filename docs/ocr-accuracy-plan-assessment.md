# Is the OCR accuracy plan lightweight, and will it actually help?

Short answers: **No, not as written — the full plan is heavy.** The tradeoff is
complexity, dependencies, and a real risk of *over-correcting* text, against an
accuracy gain that is **plausible but only proven for one part of the pipeline**.
The single biggest lever is post-OCR correction of corpus-attested text; the
image-side stages are the speculative ones.

Research method: primary sources (provider/model cards, the ICDAR post-OCR
correction competitions, the post-OCR survey) plus **this repo's own measured
results**. Date 2026-09-24.

## The evidence we already have (our own data)

| Measurement | Result |
|---|---|
| Sarvam, `raw/sample1.png` (clean, GT available) | **CER 1.54%** (22 edits / 1,429 GT chars) |
| Paddle, `raw/sample1.png` | CER 33.87% |
| Paddle, `raw/sample1.png` + filter | CER 27.22% |
| Paddle, repaired grayscale `sample1` | CER 40.10% |
| Paddle, repaired grayscale + filter | CER 31.91% |
| Sarvam on a repaired page with GT | **not measured** |

Two conclusions fall straight out:

1. **Sarvam is already near the ceiling on clean pages.** 1.54% CER leaves at most
   ~1.5 points of headroom. No amount of pipeline engineering can gain more than that
   on a page this clean — the remaining errors are the hard ones.
2. **Image repair hurt the clean page** for Paddle (27.22% → 31.91% with the repaired
   grayscale). That is direct evidence against assuming preprocessing helps. Whether it
   helps *Sarvam on damaged pages* is the real open question and is currently
   **unmeasured** because sample2 has no ground truth.

So the honest framing: the plan's upside lives on **damaged pages** and in
**post-correction**, not in re-processing clean pages.

## Is it lightweight?

Component-by-component, using measured model sizes:

| Stage | New deps | Weight | Verdict |
|---|---|---|---|
| 1. Doc-type classifier (OpenCV features + linear SVM, numpy inference) | none | ~0 MB, ~10–30 ms/page | **Lightweight** |
| 1b. …if upgraded to DINOv2-small / MobileCLIP2-S0 / EfficientNet-Lite ONNX | `onnxruntime` | 9–84 MB model | Moderate |
| 2. Damage analyser (extend `router.py`) | none | ~0 MB, ~20–50 ms | **Lightweight** |
| 3. Candidate planner (reuse `repair.py`) | none | ~0 MB local, but **2× Sarvam calls** if it votes | Costly (OCR is the expensive step) |
| 4. Sarvam OCR | existing | already **9–17 s/page** measured | The dominant cost |
| 5a–5c. Rules + confusables + lexicon/morphology | possibly Java (`tacola` analyzer) or FST build | small data, extra toolchain | Moderate |
| 5d. Tamil LM re-rank | `torch`/`transformers` | Tamil-BERT ~400 MB, or an API | **Heavy** |
| 5e. Semantic layer (encoder + `sqlite-vec`) | `onnxruntime`, `sqlite-vec` | encoder 120–600 MB + corpus vectors | **Heavy** |
| 6. Human correction | none | — | Already built |
| 7. Evaluation loop | none | — | Already mostly built |

**Verdict:** the *core* (classifier + damage analyser + rule/lexicon verifier) is
genuinely lightweight and can be numpy/OpenCV-only. The *full* plan — LM re-ranking
plus a semantic embedding layer plus morphology — is not. And latency is not the
problem: Sarvam already takes 9–17 s/page, so tens of milliseconds of local work is
noise. **The real cost is dependencies and complexity**, which the backend has so far
deliberately avoided (CPU-only, no torch).

## The tradeoffs

- **Accuracy vs over-correction.** Post-OCR correction can *introduce* errors, not just
  remove them. The ICDAR post-OCR correction competitions exist precisely because this
  is hard and bounded. Mitigation is already in our design: candidates are *suggested*,
  never silently rewritten, and every change goes through the human queue.
- **Preprocessing can hurt.** Measured, above. Any image stage must be gated and shown
  to help before it is enabled.
- **Retrieval only works for attested text.** The semantic layer can restore exact
  wording when the page is from a known corpus (Project Madurai / CICT classical Tamil).
  For an arbitrary manuscript it has no match and degrades to an anomaly flag — useful
  for the queue, but it does not raise raw accuracy.
- **Classifier risk (YAGNI).** Knowing "modern vs manuscript vs palm leaf" changes OCR
  accuracy **only if it changes a decision** (which preprocessing, which Sauvola `k`,
  which lexicon). If it just labels the page, it is cosmetic and should be cut.
- **Maintenance surface.** An LM, an embedding model, a vector index, and an FST are
  four more things to pin, license-check (GPL-2.0/3.0 for some Tamil tools), and keep
  working.

## Can it actually increase accuracy?

**Yes — but only in two places, and both must be measured.**

1. **Post-OCR correction of corpus-attested text (strongest lever).** The survey on
   post-OCR processing confirms OCR is much weaker on historical material and that
   post-correction is the established remedy, and the ICDAR competitions show real but
   bounded gains. Because our GT page is classical Tamil that exists in public corpora,
   retrieval + confusable substitution can turn "close but wrong" into exact. This is
   the part most likely to move CER, especially on damaged pages.
2. **Damage-aware preprocessing for damaged pages (unproven).** Plausible, but our only
   clean-page data says repair can hurt. Needs a GT page for sample2 before believing it.

**Probably not:** the doc-type classifier by itself (no accuracy effect unless it gates
a decision), and the semantic layer for unattested uploads (flags, not fixes).

**Ceiling check:** on `raw/sample1.png` Sarvam is at 1.54% CER, so even a perfect
verifier can only gain ~1.5 points there. The plan's value must be judged on the
**damaged/manuscript** pages, where CER is currently unknown.

## What to cut, and the kill criteria

Cut or defer until measured (YAGNI):
- the doc-type classifier — **unless** it selects a preprocessing/parameter the damage
  analyser would otherwise guess;
- the two-candidate OCR vote — costs 2× the slowest step;
- the LM re-rank and the semantic layer — build them only after the cheap
  lexicon+confusable verifier is measured to fall short.

Adopt only if it earns its place:
- every stage must reduce CER on a **frozen** eval set (`gt_cict_narrinai_p3.txt` plus
  new GT pages) by a measurable amount, or it gets deleted;
- never let a stage silently rewrite text — suggestions only;
- measure the damaged-page CER first; that is where the headroom is.

**Bottom line:** the lightweight half of the plan (damage analyser + rule/lexicon
verifier + candidate planner gated by evidence) is worth building and can plausibly
improve accuracy on damaged pages. The heavy half (LM + semantic layer) is only
justified once we have a GT page proving the cheap verifier isn't enough — and on clean
pages it cannot help, because Sarvam is already at 1.54% CER.

## Sources

- Post-OCR survey (historical material is much weaker; post-correction is the remedy): https://api.openalex.org/works?filter=title.search:Survey%20of%20Post-OCR%20Processing%20Approaches
- ICDAR 2019 Post-OCR Text Correction competition (manuscripts, historical print; bounded gains): https://api.openalex.org/works?filter=title.search:ICDAR%202019%20Competition%20on%20Post-OCR%20Text%20Correction
- ICDAR 2017 competition: https://api.openalex.org/works?filter=title.search:ICDAR2017%20Competition%20on%20Post-OCR%20Text%20Correction
- BERT/NMT post-OCR correction: https://api.openalex.org/works?filter=title.search:Neural%20Machine%20Translation%20with%20BERT%20for%20Post-OCR%20Error%20Detection%20and%20Correction
- Our own measurements: `OCR_API_COMPARISON.md`, `OCR_TEST_SCORES.md`, `ocr_outputs/`, `gt_cict_narrinai_p3.txt`
- Model sizes: HuggingFace model pages (`facebook/dinov2-small`, `Xenova/multilingual-e5-small`, `google/embeddinggemma-300m`, `l3cube-pune/tamil-bert`)
