# Two layers on top of Sarvam — proposal

> **Note (repo cleanup):** the raw scans now live at `samples/editor/sample1.png` and `sample2.png`. The repair outputs (`repair/out/`, `repair/verify/`), ground-truth text (`gt_*.txt`) and most `ocr_outputs/` artifacts referenced below were removed from the tree and are git-ignored; they are in git history (e.g. commit `3c53e7f`). Only `ocr_outputs/sarvam/raw/sample*/json/` and `.../metadata/page_001.json` remain, as test fixtures.

Sarvam's output is already strong on clean pages (CER 1.54% on
`samples/editor/sample1.png`), so the only way to add accuracy is to correct what it
gets wrong **without touching the text it gets right**. Two complementary
post-OCR layers do that: one *generates* candidates (recall), one *anchors*
text to known corpora (precision). Both are local and cheap, and both feed
the existing `suggestions[]` contract.

Research basis: the post-OCR correction literature (noisy-channel models,
ICDAR competitions, the 2021 post-OCR survey) plus **our own measured
confusions**. Date 2026-09-24.

## Why these two (and not the earlier plan)

| Layer | Role | Failure it fixes | Cost |
|---|---|---|---|
| **1. Tamil verifier** — noisy-channel correction | generate & rank candidates | character-level OCR errors on any text | word list + n-gram counts, no model |
| **2. Semantic corpus anchor** — retrieval | exact fixes + anomaly flag | errors in text that exists in a known corpus | lexical index first; embeddings optional |

They are complementary: Layer 2 fixes attested text **exactly**, Layer 1
fixes the rest **plausibly**, and neither ever rewrites text silently — they
only produce `suggestions[]`.

The earlier plan's doc-type classifier and image-variant voting are **not**
in this proposal. The classifier does not change OCR output unless it gates a
decision, and image preprocessing measurably hurt our clean page
(27.22% → 31.91% CER). Accuracy here comes from the text side.

---

## Layer 1 — Tamil verifier (noisy-channel correction)

**The classic OCR-correction formulation**: the best word is
`argmax_w P(image | w) · P(w | context)`. We already have the first term
(Sarvam's text); this layer supplies the second.

**Mechanism**
1. Tokenize Sarvam's lines into words.
2. Generate candidates per doubtful word:
   - **Confusable substitutions** from *our own measured confusions*, not a
     guess. Edit distance ≤ 1:
     - Sarvam: ந→த, ண→ள, ற→க, ன→ண, ொ→ோ, ொ→ா, ீ→ி
     - Paddle (for the fallback path): த→க, ந→க, ன→ண, ந→த, ை→வ, ை→ல, ல→ி
   - **Lexicon neighbours** from a Project Madurai word list (edit distance ≤ 1–2).
   - **Morphological variants** via `apertium-tam` (GPL-3.0) or the classical-Tamil
     analyser, so valid inflected forms are not rejected by a flat lexicon.
3. Score every candidate with a **Tamil character n-gram LM** trained on Project
   Madurai: `P(w)` plus `P(w | previous word)`. Pick the best; keep the ranked list.
4. Emit as `suggestions[]` with `source: "rules" | "lexicon" | "repair"`.

**Why it works:** every candidate is a real Tamil word/confusable, so it cannot
hallucinate foreign text. The confusable table is regenerated from corrected data,
so the layer sharpens with use.

**Evidence:** noisy-channel OCR correction is the established formulation
("OCR error correction using a noisy channel model", 2002; "A generative
probabilistic OCR model for NLP applications", 2003); Tamil-specific
post-processing exists ("…post-processing error correction technique to OCRs for
printed Tamil texts", 2014); the ICDAR post-OCR competitions and BERT/NMT
correction show the approach is competitive.

**Cost:** a word list + n-gram counts + numpy. Milliseconds per line. No GPU, no torch.

---

## Layer 2 — Semantic corpus anchor (retrieval)

**Mechanism**
1. Index a Tamil corpus — **Project Madurai** (public-domain classical texts) and
   the **CICT** classical-Tamil collection — as:
   - rung 1: an n-gram inverted index (SQLite/numpy, no model), for fuzzy line match;
   - rung 2: sentence embeddings in **`sqlite-vec`** for semantic match.
2. For each OCR line, retrieve the nearest canonical passage.
3. **High similarity →** offer the canonical wording as candidate 1 (exact fix,
   highest precision). This is the single strongest correction available.
4. **Low similarity →** flag the line as *semantically anomalous* — a review-queue
   signal that complements `score_line()`'s character-level one.
5. Re-rank Layer 1's candidates by how well they fit the retrieved context.

**Why it works:** our GT page is classical Tamil (Narrinai) that exists in public
corpora, so retrieval can turn "close but wrong" into *exactly right*. It also
tells us when the text is **not** from a known source, which is where human review
should focus.

**Evidence:** lexicon/retrieval correction is a core post-OCR technique (survey,
2021); ensemble seq2seq correction reaches SOTA on ICDAR 2019 across nine languages
(2022); RAG-style anchoring is the same retrieval mechanism applied to correction.

**Cost:** rung 1 is model-free. Rung 2 needs one ONNX encoder (e.g.
`Xenova/multilingual-e5-small`, `l3cube-pune/tamil-sentence-similarity-sbert`, or
`google/embeddinggemma-300m`) + `sqlite-vec` (Apache-2.0). Tens of milliseconds per
line — negligible next to Sarvam's 9–17 s/page.

---

## How they combine

```
Sarvam text
   → Layer 2: retrieve canonical passage
        ├─ strong match  → exact candidate, high confidence
        └─ weak match    → anomaly flag
   → Layer 1: confusables + lexicon + morphology → candidates
   → re-rank by LM + retrieved context
   → suggestions[]  (never a silent rewrite)
   → human queue
```

## Cost summary

| Item | New dep | Weight | Latency |
|---|---|---|---|
| Layer 1 | none (`numpy`, `cv2` present) | word list + n-gram counts, few MB | ms/line |
| Layer 2 rung 1 (n-gram index) | none (SQLite) | corpus-sized | ms/line |
| Layer 2 rung 2 (embeddings) | `onnxruntime`, `sqlite-vec` | encoder 120–600 MB | ~10s of ms/line |
| (optional) multi-pass consensus | none | — | **2× Sarvam** — defer |

Both layers are post-OCR and local, so they do not slow the slow step. Ship rung 1
of each first; add the encoder only if retrieval quality demands it.

## Acceptance tests (kill criteria)

Every layer must earn its place on a **frozen** eval set:
- **Layer 1:** top-1 candidate equals the human correction for ≥ X% of flagged words,
  with **no increase in errors on already-clean lines** (precision matters more than
  recall). Track accepted-vs-rejected suggestions from the correction log.
- **Layer 2:** recall@1 of the canonical passage on attested pages; adopting it must
  reduce CER. On unattested pages, the anomaly flag must correlate with real errors.
- **Both:** CER before/after on `gt_cict_narrinai_p3.txt` and new GT pages. Delete any
  layer that does not move CER.

## Prerequisite (do this first)

**Get ground truth for damaged pages** (e.g. `samples/editor/sample2.png`). Sarvam is at 1.54%
CER on the clean page, so post-correction can gain at most ~1.5 points there. The
layers matter on damaged/manuscript pages, and right now those have **no GT**, so the
work is untestable where it counts. One more hand-corrected damaged page is worth more
than either layer.

Also note the hard limit: **post-correction cannot recover a character Sarvam never
saw.** If damaged pages are unreadable, the fix is better input/OCR, not correction.

## Sources

- Noisy channel: "OCR error correction using a noisy channel model" (2002); "A generative probabilistic OCR model for NLP applications" (2003) — via https://api.openalex.org
- Tamil post-processing: "A performance comparison and post-processing error correction technique to OCRs for printed Tamil texts" (2014)
- Survey: "Survey of Post-OCR Processing Approaches" (2021, 278 cites)
- Ensembles: "Post-OCR Document Correction with Large Ensembles of Character Sequence-to-Sequence Models" (2022); "Improving OCR Accuracy on Early Printed Books by combining Pretraining, Voting, and Active Learning" (2018)
- Neural correction: "Neural Machine Translation with BERT for Post-OCR Error Detection and Correction" (2020)
- Competitions: ICDAR 2017 / 2019 Post-OCR Text Correction
- Corpus: Project Madurai https://www.projectmadurai.org/ ; CICT classical Tamil collection
- Confusion table: derived from `gt_cict_narrinai_p3.txt` vs `ocr_outputs/` (this repo)
- Existing contract: `schema/suggestions-contract.md`
