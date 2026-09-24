# OCR routing and workflow (`accuracy-optimization`)

How a page goes from upload to contract JSON, which engine reads it, and what
happens when an engine fails. Two hosted engines only — **no local model is
installed**.

## Pipeline

```
Upload (PDF / PNG / JPG / TIFF / WEBP / ZIP)
  │  main.py: validate type+extension, cap size, 400 before any job row
  ▼
Hash + store master (storage.py, SHA-256 before write, verified after)
  ▼
Load pages (pdfutil.py: pypdfium2 for PDF, cv2 for images/TIFF; long side ≤ 1600 px)
  ▼
Quality scores (router.py: blur, contrast, noise, skew_deg)  → FAST | HEAVY badge
  ▼
Preprocess (preprocess.py, on a COPY — geometry + background only)
  │  crop dark scan borders → deskew → grayscale background flatten
  │  → upscale only if low-res; returns a Mapper back to the 1600 px page
  ▼
OCR, routed by the badge (ocr.py::ocr_page)
  │  FAST  → Gemini  (fallback: Sarvam)
  │  HEAVY → Sarvam  (fallback: Gemini)
  │  both fail → marked [stub] line (confidence 0.0, needs_review)
  ▼
Map boxes back to the page (Mapper.to_page)
  ▼
Contract JSON (schema_out.py) → SQLite → GET /jobs/{id}, editor, exports
```

## Model routing

| Badge | Engine | Model | Why |
|---|---|---|---|
| **FAST** | Gemini | `gemini-3.5-flash-lite` (override: `GEMINI_MODEL`) | fast, cheap, good on clean pages |
| **HEAVY** | Sarvam | Document AI "Digitise" (Sarvam Vision, `ta-IN`) | stronger on damaged/aged pages |

Routing is per page, from `router.py`'s four CV metrics:

| Metric | FAST requires |
|---|---|
| blur (Laplacian variance) | ≥ 80 |
| contrast (Otsu ink/paper) | ≥ 0.20 |
| noise (median residual std) | ≤ 15 |
| skew_deg | ≤ 7 |

`HEAVY` otherwise. Thresholds live in one place: `router.py::THRESHOLDS`.

**Fallback is mutual.** `ocr.py::_route()` returns `("gemini", "sarvam")` for
FAST and `("sarvam", "gemini")` for HEAVY; `ocr_page` tries each in order and
returns the first success. With no profile, `OCR_ENGINE` picks a single engine
(`sarvam` default, or `gemini`) with no fallback.

## Engines

### Gemini (FAST route) — `ocr.py::_gemini_ocr`
- `POST …/v1beta/models/{model}:generateContent` with the page as inline PNG.
- Prompt: transcribe Tamil verbatim, line breaks preserved, text only.
- **Returns text, not coordinates.** Line bboxes are approximated by
  `_lines_with_boxes()`: a horizontal projection profile finds text-line bands
  (`_line_bands`); if the band count matches the line count they are paired 1:1,
  otherwise lines are spread evenly across the detected text region.
- **Confidence** comes from `textcheck.score_line()` (Tamil share, orphan vowel
  signs, odd characters, repetition) — the same 0..1 proxy used elsewhere,
  because Gemini returns no per-line score.
- Failure modes handled by the fallback: no key, HTTP 429/503, and any
  `finishReason` other than `STOP` (`MAX_TOKENS`, `SAFETY`, `RECITATION` -
  the last seen on classical Tamil), even when some text came back, since
  that text is truncated or withheld.

### Sarvam (HEAVY route) — `ocr.py::_sarvam_ocr`
- `POST /doc-ai/v1/job/digitise` → poll `/status` → `/download-url` → ZIP with
  `metadata/page_NNN.json`. 429/503 are retried with backoff.
- Returns layout **blocks** (often a whole paragraph), not lines. Each block's
  text is split on newlines and the block box is divided evenly top-to-bottom,
  so line boxes are approximate and every line inherits its block's layout
  confidence (not per-line recognition certainty).
- Coordinates are rescaled from Sarvam's reported image size to ours.

### Stub — `ocr.py::_stub_lines`
When both engines fail (or no key is set), the page gets one `[stub]` line per
engine attempt with `confidence 0.0`. `schema_out` flags it `needs_review`, and
the text/PDF/DOCX exports skip it, so placeholder text is never presented as
real OCR.

## Preprocessing (safe wins only)

`preprocess.py::prepare(img) -> (prepared_bgr, Mapper)`. Runs on a copy; the
original page is never mutated.

| Step | Kind | Notes |
|---|---|---|
| Crop dark scan borders | geometry | only rows/cols ≥ 97% dark, max 12% per side |
| Deskew | geometry | projection profile, ±5°, 0.5° then 0.1°; < 0.2° is left alone |
| Background flatten | photometric | divide out paper shading; keeps pulli dots/thin strokes |
| Upscale | geometry | only when long side < 1000 px, capped 2× |

**Deliberately excluded:** denoise, binarization, and AI restoration. These
models clean up internally and the repo's own measurement showed aggressive
repair hurts clean pages (Paddle raw+filter CER 27.22% vs repaired+filter
31.91%). See `docs/ocr-accuracy-plan-assessment.md`.

**Box contract:** because cropping/deskewing/resizing move pixels,
`Mapper.to_page()` applies the inverse transform to each OCR bbox and returns
the axis-aligned box on the original 1600 px page. The frontend contract is
therefore unchanged.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` | (none) | required for the FAST route |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | Gemini vision model |
| `GEMINI_LANGUAGE` | `Tamil` | prompt language name |
| `GEMINI_TIMEOUT_S` | `120` | per-page HTTP budget |
| `SARVAM_API_KEY` | (none) | required for the HEAVY route |
| `SARVAM_LANGUAGE` | `ta-IN` | document language |
| `SARVAM_TIMEOUT_S` | `120` | per-page budget (submit + poll + download) |
| `SARVAM_POLL_S` | `3` | status poll interval |
| `SARVAM_BASE_URL` | `https://api.sarvam.ai` | override for testing |
| `OCR_ENGINE` | `sarvam` | `sarvam` or `gemini` when no page profile is given |

Keys are read from the environment at call time, never logged, and scrubbed
from error text by `_safe_error()`. A git-ignored repo-root `.env` loads with
`uvicorn app.main:app --env-file ../../.env`.

`GET /health` reports `ocr_engine` (`gemini` | `sarvam` | `stub`),
`ocr_engine_selected`, `gemini_key_set` / `sarvam_key_set`, and the last
`gemini_error` / `sarvam_error`.

## Key files

| File | Role |
|---|---|
| `app/main.py` | pipeline: load → score/route → prepare → `ocr_page(prepared, profile)` → `mapper.to_page` → contract |
| `app/router.py` | FAST/HEAVY metrics and thresholds |
| `app/preprocess.py` | safe-wins cleanup + `Mapper` |
| `app/ocr.py` | Gemini + Sarvam engines, routing, fallbacks, `/health` |
| `app/textcheck.py` | `score_line()` text-quality confidence proxy |
| `app/schema_out.py` | frozen contract JSON |

## Tests

`python -m pytest tests/ -v` — no network, no keys (cleared in `conftest.py`).

- `tests/test_preprocess.py` — crop/deskew/flatten round-trip back to page coords
- `tests/test_ocr_routing.py` — FAST→Gemini, HEAVY→Sarvam, Gemini-fail→Sarvam
- `tests/test_sarvam_ocr.py` — Sarvam HTTP flow (mocked), stub on failure, error scrubbing
- `tests/test_demo_hardening.py` — one page failing does not sink the job

## Known limitations

- **Gemini line boxes are approximate** (page-wide bands from the ink profile),
  not pixel-tight.
- **Gemini is a preview route**: free-tier rate limits (429) and
  `finishReason=RECITATION` on classical Tamil; both fall back to Sarvam.
- **Sarvam line boxes are approximate** (even block split).
- **No offline mode**: both engines are hosted; with no network/key the app
  serves `[stub]` output.
- `preprocess.py` heuristics are intentionally simple; each constant is one
  place to tune (marked `ponytail:` in the code).
