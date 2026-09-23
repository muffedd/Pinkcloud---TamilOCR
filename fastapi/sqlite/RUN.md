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

## 3. POST a scanned page

```bash
curl -s -X POST http://127.0.0.1:8000/jobs -F "file=@scan.jpg"
# → {"job_id": "af3c8e14..."}

curl -s http://127.0.0.1:8000/jobs/af3c8e14...
# → {"job_id": "...", "status": "done", "result": {"pages": [ ... contract JSON ... ]}}
```

Accepted types: pdf, jpg, jpeg, png, tiff (anything else → HTTP 400).

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
