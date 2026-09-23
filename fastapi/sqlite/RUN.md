# Pink Cloud — run the backend

Tamil OCR web app backend: upload → hash → route → OCR → frozen contract JSON.

## 1. Install (Windows, CPU only)

Requires **Python 3.11-3.13**: numpy 2.3.5 needs 3.11+, and the optional
paddlepaddle 3.3.1 has no wheels past 3.13. Check with `python --version`
(on Windows: `py -3.12 -m venv .venv` picks a specific version).

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Optional real OCR (big download, everything else works without it):

```bat
pip install paddlepaddle paddleocr
```

## 2. Run the server

```bat
uvicorn app.main:app --reload
```

- http://127.0.0.1:8000/health → `{"ok": true}`
- http://127.0.0.1:8000/docs → auto-generated Swagger UI

## OCR engine: Sarvam (default) or PaddleOCR (offline fallback)

The OCR pass calls the **Sarvam Document AI Digitise API** (Sarvam Vision,
`ta-IN`) by default. Set the key as an environment variable on the machine
that runs the server. Never commit it and never paste it into chat.

| Variable | Default | Meaning |
|---|---|---|
| `SARVAM_API_KEY` | (none) | Sarvam API subscription key. Required for Sarvam. |
| `OCR_ENGINE` | `sarvam` | `sarvam` or `paddle`. `paddle` never calls the network. |
| `SARVAM_FALLBACK` | `paddle` | If the key is missing or a Sarvam call fails, run local PaddleOCR for that page. `none` returns the marked `[stub]` page instead. |
| `SARVAM_LANGUAGE` | `ta-IN` | Document language sent to Sarvam. |
| `SARVAM_TIMEOUT_S` | `120` | Time budget per page (submit + poll + download). |
| `SARVAM_POLL_S` | `3` | Seconds between status polls. |
| `SARVAM_BASE_URL` | `https://api.sarvam.ai` | Override only for testing. |

```bat
:: Windows (current terminal only)
set SARVAM_API_KEY=<paste key here, locally>
uvicorn app.main:app

:: Offline demo, no network
set OCR_ENGINE=paddle
uvicorn app.main:app
```

```bash
# macOS / Linux
export SARVAM_API_KEY='<paste key here, locally>'
uvicorn app.main:app
```

`GET /health` shows which engine ran the last page (`ocr_engine`: `sarvam`,
`paddle` or `stub`), what was selected (`ocr_engine_selected`), whether a key
is present (`sarvam_key_set`, never the key itself), and the last Sarvam
error (`sarvam_error`).

How Sarvam output maps onto the contract:

- Sarvam returns layout **blocks** (often a whole paragraph), not lines. Each
  block's text is split on newlines into contract lines, and the block box is
  divided evenly top to bottom. Line boxes are therefore approximate.
- Every line gets its block's `confidence`. That is Sarvam's layout score
  (observed 0.30-0.91 on real pages), not per-line recognition certainty, and
  it runs much lower than PaddleOCR's scores. The editor's Doubt/heatmap
  thresholds were tuned for PaddleOCR.
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
- Without paddle installed you get a `[stub]` line with confidence 0.0 —
  same shape, so the frontend can be built against it.

## 5. Real OCR requirements

- `pip install "paddlepaddle==3.3.1" "paddleocr==3.7.0"` (Python <= 3.13; no 3.14 wheels).
- paddlepaddle 3.3.1 wheels need an **AVX-capable CPU**. On machines without AVX,
  the backend detects this at startup (subprocess probe), logs an ERROR and serves
  marked stub output — `/health` reports `"ocr_engine": "stub"` with the reason.
- Engine flags set for correctness: `enable_mkldnn=False` (paddle 3.3.1 CPU
  NotImplementedError in predict), `use_doc_orientation_classify=False`,
  `use_doc_unwarping=False` (unwarping breaks the bbox-on-1600px contract).

## 6. Threshold tuning (CICT samples)

```bash
python tools/tune_thresholds.py <folder-with-CICT-samples>
```

Prints the 4 metrics per sample and suggested `THRESHOLDS`; paste the result
into `app/router.py`. Current defaults: blur>=80, contrast>=0.20, noise<=15,
skew_deg<=7. Tests: `python -m pytest tests/ -v` (real-OCR e2e auto-skips when
paddle is unusable).

## 7. Notes

- Uploads are stored byte-for-byte at `uploads/<job_id>/master.<ext>`,
  SHA-256 hashed before writing and verified after.
- DB is a single SQLite file: `pinkcloud.db`. Delete it to reset jobs.
- Tune routing thresholds in `app/router.py` (`THRESHOLDS` dict).
