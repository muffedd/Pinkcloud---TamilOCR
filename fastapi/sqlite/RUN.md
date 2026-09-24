# Pink Cloud — run the backend

Tamil OCR web app backend: upload → hash → route → OCR → frozen contract JSON.

## 1. Install (Windows, CPU only)

Requires **Python 3.11+**: numpy 2.3.5 needs 3.11+. Check with `python --version`
(on Windows: `py -3.12 -m venv .venv` picks a specific version).

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Run the server

```bat
uvicorn app.main:app --reload
```

- http://127.0.0.1:8000/health → `{"ok": true}`
- http://127.0.0.1:8000/docs → auto-generated Swagger UI

## OCR routing: Gemini (FAST) + Sarvam (HEAVY)

Pages are routed by the `router.py` FAST/HEAVY badge:

- **FAST** -> **Gemini** (`gemini-3.5-flash-lite` by default), the fast route.
- **HEAVY** -> **Sarvam** Document AI Digitise (Sarvam Vision, `ta-IN`).
- If the routed engine fails (missing key, rate limit, RECITATION, network) the
  other engine is tried; if both fail the page gets marked `[stub]` lines
  (confidence 0, flagged for review) and the rest of the job still runs.
- There is no local/offline OCR engine. `SARVAM_FALLBACK` from the old PaddleOCR
  setup is ignored (a warning is logged at startup).

Before OCR, `preprocess.py` applies safe-wins cleanup on a copy: crop dark scan
borders, deskew, grayscale background flattening, and upscale only when the scan
is low-res. Boxes are mapped back to the page, so the contract is unchanged.

Set keys as environment variables on the machine that runs the server. Never
commit them. The git-ignored repo-root `.env` is supported via uvicorn's
`--env-file`; a variable already present in the environment always wins.

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` | (none) | Gemini API key. Required for the FAST route. |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | Gemini vision model for OCR. |
| `GEMINI_LANGUAGE` | `Tamil` | Language name used in the OCR prompt. |
| `GEMINI_TIMEOUT_S` | `120` | Per-page HTTP budget. |
| `SARVAM_API_KEY` | (none) | Sarvam API subscription key. Required for the HEAVY route. |
| `OCR_ENGINE` | `sarvam` | `sarvam` or `gemini` when no page profile is given. |
| `SARVAM_LANGUAGE` | `ta-IN` | Document language sent to Sarvam. |
| `SARVAM_TIMEOUT_S` | `120` | Time budget per page (submit + poll + download). |
| `SARVAM_POLL_S` | `3` | Seconds between status polls. |
| `SARVAM_BASE_URL` | `https://api.sarvam.ai` | Override only for testing. |

```bat
:: Windows (current terminal only)
set GEMINI_API_KEY=<paste key here, locally>
set SARVAM_API_KEY=<paste key here, locally>
uvicorn app.main:app --env-file ..\..\.env
```

```bash
# macOS / Linux - load the git-ignored .env (from fastapi/sqlite/)
uvicorn app.main:app --env-file ../../.env

# or export explicitly
export GEMINI_API_KEY='<paste key here, locally>'
export SARVAM_API_KEY='<paste key here, locally>'
uvicorn app.main:app
```

`GET /health` shows which engine ran the last page (`ocr_engine`: `gemini`,
`sarvam` or `stub`), what was selected (`ocr_engine_selected`), whether each key
is present (`gemini_key_set` / `sarvam_key_set`, never the keys themselves), and
the last per-engine error (`gemini_error` / `sarvam_error`).

How Sarvam output maps onto the contract:

- Sarvam returns layout **blocks** (often a whole paragraph), not lines. Each
  block's text is split on newlines into contract lines, and the block box is
  divided evenly top to bottom. Line boxes are therefore approximate.
- Sarvam's per-block `confidence` is a layout score (observed 0.30-0.91 on
  real pages), not per-line recognition certainty. It is kept on each line as
  `layout_confidence` for reference only. The line `confidence` is the text
  check `app/textcheck.py` `score_line()`: a text-quality proxy (how malformed
  the text looks), not a recognition probability. A line is flagged
  `needs_review` when it scores below 0.80 or is `[stub]` output, matching
  the editor's bands (Auto >= 0.95, OK 0.80-0.95, Doubt < 0.80). A HEAVY page
  keeps its page-level flag but only its bad lines are flagged.
- Each page is one Sarvam job (about 10-17 s observed). Sarvam's Document
  Intelligence rate limit is 10 requests/minute on every plan, and each page
  uses several requests (submit, status polls, download link), so multi-page
  PDFs are slow; 429s are retried with backoff.

Tests never call Sarvam or Gemini: `tests/conftest.py` removes both keys and
`tests/test_sarvam_ocr.py` uses a mocked HTTP transport.

## AI fix (optional, Gemini)

`POST /jobs/{id}/suggest` answers 503 until `GEMINI_API_KEY` is set (model
`gemini-3.1-flash-lite`; override with `SUGGEST_MODEL`, disable with
`SUGGEST_PROVIDER=off`). Set the key in the server environment only, never in
code or chat. Details: `schema/ai-fix-contract.md`. Tests never call Gemini.

## 3. POST a scanned page

```bash
curl -s -X POST http://127.0.0.1:8000/jobs -F "file=@scan.jpg"
# → {"job_id": "af3c8e14..."}

curl -s http://127.0.0.1:8000/jobs/af3c8e14...
# → {"job_id": "...", "status": "done", "result": {"pages": [ ... contract JSON ... ]}}
```

Accepted types: pdf, jpg, jpeg, png, tiff, webp (anything else → HTTP 400).

### Multi-image job (several photos → one job)

Repeat the `files` field instead of sending `file`. Images are kept in
upload order, one page per image (page 1 = first file). PDFs are
single-file only.

```bash
curl -s -X POST http://127.0.0.1:8000/jobs \
  -F "files=@p1.jpg" -F "files=@p2.png" -F "files=@p3.webp"
```

Each image is stored byte-for-byte as `master-001.<ext>`, `master-002.<ext>`, ...
The job's `sha256` is SHA-256 over the per-image SHA-256 hex digests joined
by newlines, in page order. Sending both `file` and `files` → 400.

## 4. The output contract (frozen)

```json
{
  "job_id": "...",
  "status": "done",
  "sha256": "=2f1a...",
  "result": {
    "pages": [
      {
        "page": 1,
        "profile": "FAST",
        "quality": {"blur": 412.7, "contrast": 0.51, "noise": 6.2, "skew_deg": 0.4},
        "lines": [
          {"id": "L1", "seq": 1, "body": "தமிழ் உரை", "bbox": [120, 88, 900, 40], "confidence": 0.96}
        ],
        "text": "தமிழ் உரை",
        "needs_review": false,
        "processing_ms": 812
      }
    ]
  }
}
```

- Each page is scored with 4 metrics (blur, contrast, noise, skew_deg) then
  badged **FAST** (clean) or **HEAVY** (damaged → repair pass later).
- Bboxes are on the 1600px-capped image; draw them directly on the frontend.
- Without a Sarvam key (or when Sarvam fails) you get `[stub]` lines with
  confidence 0.0 — same shape, so the frontend can be built against it.

## 5. Real OCR requirements

- A Sarvam API key in `SARVAM_API_KEY` (see "OCR engine: Sarvam" above) and
  network access to `api.sarvam.ai`. Nothing else to install.
- Without a key, `/health` reports `"ocr_engine": "stub"`,
  `"sarvam_key_set": false` and an `ocr_error`, and pages come back as marked
  stub output.

## 6. Threshold tuning (CICT samples)

```bash
python tools/tune_thresholds.py <folder-with-CICT-samples>
```

Prints the 4 metrics per sample and suggested `THRESHOLDS`; paste the result
into `app/router.py`. Current defaults: blur>=80, contrast>=0.20, noise<=15,
skew_deg<=7. Tests: `pip install -r requirements-dev.txt` (adds pytest), then
`python -m pytest tests/ -v` (offline; Sarvam is mocked).

## 7. Notes

- Uploads are stored byte-for-byte at `uploads/<job_id>/master.<ext>`,
  SHA-256 hashed before writing and verified after.
- DB is a single SQLite file: `pinkcloud.db`. Delete it to reset jobs.
- Tune routing thresholds in `app/router.py` (`THRESHOLDS` dict).
