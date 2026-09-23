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

## OCR engine: Sarvam

The OCR pass calls the **Sarvam Document AI Digitise API** (Sarvam Vision,
`ta-IN`). It is the only OCR engine. Set the key as an environment variable on the machine
that runs the server. Never commit it and never paste it into chat.

| Variable | Default | Meaning |
|---|---|---|
| `SARVAM_API_KEY` | (none) | Sarvam API subscription key. Required for Sarvam. |
| `SARVAM_LANGUAGE` | `ta-IN` | Document language sent to Sarvam. |
| `SARVAM_TIMEOUT_S` | `120` | Time budget per page (submit + poll + download). |
| `SARVAM_POLL_S` | `3` | Seconds between status polls. |
| `SARVAM_BASE_URL` | `https://api.sarvam.ai` | Override only for testing. |

```bat
:: Windows (current terminal only)
set SARVAM_API_KEY=<paste key here, locally>
uvicorn app.main:app
```

```bash
# macOS / Linux
export SARVAM_API_KEY='<paste key here, locally>'
uvicorn app.main:app
```

If the key is missing or a Sarvam call fails, that page gets marked `[stub]`
lines (confidence 0, flagged for review) and the rest of the job still runs.
There is no local/offline OCR engine. `OCR_ENGINE` and `SARVAM_FALLBACK` from
the old PaddleOCR setup are ignored (a warning is logged at startup).

`GET /health` shows which engine ran the last page (`ocr_engine`: `sarvam`
or `stub`), what was selected (`ocr_engine_selected`, always `sarvam`), whether a key
is present (`sarvam_key_set`, never the key itself), and the last Sarvam
error (`sarvam_error`).

How Sarvam output maps onto the contract:

- Sarvam returns layout **blocks** (often a whole paragraph), not lines. Each
  block's text is split on newlines into contract lines, and the block box is
  divided evenly top to bottom. Line boxes are therefore approximate.
- Every line gets its block's `confidence`. That is Sarvam's layout score
  (observed 0.30-0.91 on real pages), not per-line recognition certainty.
  The editor's Doubt/heatmap thresholds were tuned before the switch to
  Sarvam and may need retuning for these lower scores.
- Each page is one Sarvam job (about 10-17 s observed). Sarvam's Document
  Intelligence rate limit is 10 requests/minute on every plan, and each page
  uses several requests (submit, status polls, download link), so multi-page
  PDFs are slow; 429s are retried with backoff.

Tests never call Sarvam: `tests/conftest.py` removes `SARVAM_API_KEY` and
`tests/test_sarvam_ocr.py` uses a mocked HTTP transport.

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
skew_deg<=7. Tests: `python -m pytest tests/ -v` (offline; Sarvam is mocked).

## 7. Notes

- Uploads are stored byte-for-byte at `uploads/<job_id>/master.<ext>`,
  SHA-256 hashed before writing and verified after.
- DB is a single SQLite file: `pinkcloud.db`. Delete it to reset jobs.
- Tune routing thresholds in `app/router.py` (`THRESHOLDS` dict).
