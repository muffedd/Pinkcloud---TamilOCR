# Pink Cloud — Tamil OCR

Pink Cloud is an early-stage tool for reading scanned Tamil and English books. Its planned workflow repairs low-quality pages, supports human corrections, and exports searchable text.

## Status

Design foundations are done. The backend application (FastAPI + SQLite, CPU-only) is written and running — see `fastapi/sqlite/`.

## Application — FastAPI + SQLite backend

The pipe-skeleton-router backend lives in `fastapi/sqlite/app/`. Python 3, CPU only, runs on Windows with no Docker/poppler/torch.

| File | Role |
| --- | --- |
| `app/main.py` | FastAPI endpoints: `GET /health`, `POST /jobs`, `GET /jobs/{job_id}` |
| `app/db.py` | SQLite jobs table (`init_db`, `create_job`, `get_job`, `set_result`) |
| `app/storage.py` | Byte-for-byte masters: SHA-256 hashed before write, verified after |
| `app/router.py` | Per-page quality metrics (blur, contrast, noise, skew_deg) → **FAST** / **HEAVY** badge |
| `app/pdfutil.py` | PDF via pypdfium2, images via cv2; long side capped at 1600 px |
| `app/ocr.py` | Lazy PaddleOCR fast pass (PP-OCRv5_mobile_det + ta_PP-OCRv5_mobile_rec); stub result if paddle is absent |
| `app/schema_out.py` | Builds the frozen output contract JSON (`schema/schema.json`) |

### Run it

```bash
pip install -r fastapi/sqlite/requirements.txt
cd fastapi/sqlite
uvicorn app.main:app --reload
```

PaddleOCR is optional — without it the API returns stub OCR lines with the same contract, so the frontend can be built against it today.

### Quick test

```bash
curl -s http://127.0.0.1:8000/health                     # {"ok": true}
curl -s -X POST http://127.0.0.1:8000/jobs -F "file=@scan.png"   # {"job_id": "..."}
curl -s http://127.0.0.1:8000/jobs/<job_id>              # status + result JSON
```

Full details in [`fastapi/sqlite/RUN.md`](fastapi/sqlite/RUN.md).

## Design system

| Folder | Contents |
| --- | --- |
| `design system/foundations/` | Color systems, typography specimen, design brief, and shared styles. |
| `design system/components/` | Component sheet, library, and standalone button/toggle examples. |
| `design system/layout/` | Three-page layout plan. |

Open the HTML design files in a browser. No build step is needed.

## Product direction

- Upload multi-page PDF and image scans, including TIFF.
- Run a fast OCR pass on every page and send doubtful pages through repair and heavier OCR.
- Review the scan beside Unicode Tamil text; make corrections explicit and traceable.
- Export searchable PDF, TXT, and a processing receipt.
- Keep runtime assets local. Any hosted OCR route remains a project decision.

## Project references

- `design system/foundations/design-system-spec.txt` — design system brief.
- `schema/schema.json` and `schema/doc_demo.json` — OCR output contract and demo page.

