# Pink Cloud - API endpoints

Contract for the FastAPI backend in `fastapi/sqlite/app/main.py` (app version `0.2.0`).
Base URL when run locally (see `fastapi/sqlite/RUN.md`): `http://127.0.0.1:8000`

| Method | Path | What it does |
|---|---|---|
| GET | `/health` | Liveness + OCR engine state |
| POST | `/jobs` | Upload a document, process it, get a `job_id` |
| GET | `/jobs/{job_id}` | Job status, hash, and page results |
| GET | `/docs` | Auto-generated Swagger UI (FastAPI) |

## Processing is synchronous

`POST /jobs` does the whole pipeline (validate → hash + store → route → OCR → contract JSON)
before it responds. When you get a `job_id` back, the job is already `done` or `error`,
so one `GET /jobs/{job_id}` is enough. No polling needed today; if this moves to a
background task later, poll until `status` is not `pending`.

## GET /health

```bash
curl -s http://127.0.0.1:8000/health
# → {"ok": true, "ocr_engine": "paddle"}
```

| Field | Type | Notes |
|---|---|---|
| `ok` | bool | Always `true` if the server is up |
| `ocr_engine` | string | `paddle` (real OCR), `stub` (fallback, marked output), or `not_initialized` |
| `ocr_error` | string | Only present when engine init failed: last line of the error |

The engine is probed at startup. If paddle is missing or unusable (e.g. no AVX CPU),
the API still works and returns stub lines - check `/health` before trusting OCR text.

```json
{"ok": true, "ocr_engine": "stub", "ocr_error": "..."}
```

## POST /jobs

Multipart upload, one file in the form field `file`.

```bash
curl -s -X POST http://127.0.0.1:8000/jobs -F "file=@scan.jpg"
# → {"job_id": "af3c8e14..."}
```

Validation runs in this order, before any job row is created:

1. **Type AND extension** - both must be supported. A mismatch on either side → `400`.
2. **Decodable bytes** - the PDF must open / the image must decode → else `422`.

| Accepted content type | Accepted extensions |
|---|---|
| `application/pdf` | `.pdf` |
| `image/jpeg` | `.jpg`, `.jpeg` |
| `image/png` | `.png` |
| `image/tiff` | `.tif`, `.tiff` |

The check is two independent sets: content type in the list AND extension in the list
(case-insensitive). So `evil.exe` sent as `image/png` → `400`. Multi-page PDFs and
multi-page TIFFs produce one result page per page.

| Status | Body | When |
|---|---|---|
| `200` | `{"job_id": "<32-char hex>"}` | Accepted and processed (check `status` - it can still be `error`) |
| `400` | `{"detail": "unsupported file type: use pdf, jpg, jpeg, png or tiff"}` | Bad content type or extension |
| `422` | `{"detail": "file could not be decoded (corrupt or empty document)"}` | Corrupt or empty file |
| `422` | FastAPI validation error (`detail` is a list) | `file` field missing from the form |

## GET /jobs/{job_id}

```bash
curl -s http://127.0.0.1:8000/jobs/af3c8e14...
```

```json
{
  "job_id": "af3c8e14...",
  "filename": "scan.jpg",
  "sha256": "2f1a...",
  "status": "done",
  "created_at": "2026-09-23T10:38:56.123456+00:00",
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

| Field | Type | Notes |
|---|---|---|
| `job_id` | string | 32-char hex UUID; also the folder name under `uploads/` |
| `filename` | string | Original upload name |
| `sha256` | string | Hex SHA-256 of the uploaded bytes, taken before writing to disk |
| `status` | string | `pending` \| `done` \| `error` |
| `created_at` | string | ISO 8601, UTC |
| `result` | object \| null | `{"pages": [...]}` when `done`; `{"error": "<message>"}` when `error`; `null` while `pending` |

| Status | Body | When |
|---|---|---|
| `200` | Job object above | Job exists |
| `404` | `{"detail": "job not found"}` | Unknown `job_id` |

### result.pages[] - page contract

Each page follows `schema/schema.json` (CICT line-based fields, `additionalProperties: false`).

Required on every page:

| Field | Type | Notes |
|---|---|---|
| `page` | int | 1-based page number |
| `profile` | `"FAST"` \| `"HEAVY"` | Router badge: clean vs damaged page |
| `quality` | object | `blur`, `contrast`, `noise`, `skew_deg` (all numbers, required) |
| `lines` | array | Line records in reading order; `[]` for unreadable pages |
| `text` | string | Line `body` values joined with `\n` in `seq` order |

Each `lines[]` item:

| Field | Type | Notes |
|---|---|---|
| `id` | string | `L1`, `L2`, ... |
| `seq` | int | 1-based reading order (top-to-bottom, left-to-right within a line band) |
| `body` | string | Recognized Tamil line text |
| `bbox` | `[x, y, w, h]` | Pixels on the 1600px-capped image; draw directly on the frontend |
| `confidence` | number | 0-1; the UI colors each line by this |

Also emitted by the current backend:

| Field | Type | Notes |
|---|---|---|
| `needs_review` | bool | `true` if `profile` is `HEAVY`, no lines, or best line confidence < 0.5 |
| `processing_ms` | int | Wall-clock ms for this page |

Allowed by the schema but **not emitted yet**: `preprocessed`, `words[]`, `corrections[]`,
`verdicts[]`, CICT identity fields (`specimen`, `chapter`, `work`, `manuscript_id`, `script`,
`material`, `license`, `doi`), extra `quality` keys (`damage_flags`, `ink_density`,
`estimated_lines`, `quality_score`) and extra line keys (`kural`, `margin`, `numeral`,
`end_char`). Clients should tolerate them appearing later.

### Stub output

With `ocr_engine: "stub"`, each page gets placeholder lines whose `body` starts with `[stub]` and
`confidence` is `0.0` - same shape, so the frontend can be built against it. Such pages
always have `needs_review: true`.

## Swagger

http://127.0.0.1:8000/docs → interactive Swagger UI generated by FastAPI; try `POST /jobs`
from the browser. The raw OpenAPI spec is at `/openapi.json`.
