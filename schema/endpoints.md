# Pink Cloud - API endpoints

Contract for the FastAPI backend in `fastapi/sqlite/app/main.py` (app version `0.2.0`).
Base URL when run locally (see `fastapi/sqlite/RUN.md`): `http://127.0.0.1:8000`

| Method | Path | What it does |
|---|---|---|
| GET | `/health` | Liveness + OCR engine state |
| POST | `/jobs` | Upload a document, process it, get a `job_id` |
| GET | `/jobs/{job_id}` | Job status, hash, and page results |
| GET | `/jobs` | Job list (newest first, paginated) |
| GET | `/search?q=` | Full-text search over the FINAL (corrections-applied) line text of done jobs |
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
| `needs_review` | bool | Per-line review flag: confidence < 0.5, `[stub]` body, or a `HEAVY` page (same rule the receipt counts by). Back-filled on read for jobs stored before this field existed |

Also emitted by the current backend:

| Field | Type | Notes |
|---|---|---|
| `needs_review` | bool | `true` if `profile` is `HEAVY`, no lines, or best line confidence < 0.5 |
| `processing_ms` | int | Wall-clock ms for this page |

Allowed by the schema but **not emitted yet**: `preprocessed`, `words[]`, `corrections[]`,
`verdicts[]`, `suggestions[]` (page-level review candidates, shape per
`schema/suggestions-contract.md`; passed through untouched when the HEAVY repair
fix-list/dictionary module emits them, absent until then), CICT identity fields
(`specimen`, `chapter`, `work`, `manuscript_id`, `script`,
`material`, `license`, `doi`), extra `quality` keys (`damage_flags`, `ink_density`,
`estimated_lines`, `quality_score`) and extra line keys (`kural`, `margin`, `numeral`,
`end_char`). Clients should tolerate them appearing later.

### Stub output

With `ocr_engine: "stub"`, each page gets placeholder lines whose `body` starts with `[stub]` and
`confidence` is `0.0` - same shape, so the frontend can be built against it. Such pages
always have `needs_review: true`.

## GET /jobs

Newest-first job list for the Library page. `?limit=` (1-200, default 50) and `?offset=`
paginate; `total` is the full job count.

```json
{"total": 1, "limit": 50, "offset": 0, "jobs": [
  {"job_id": "af3c...", "filename": "scan.jpg", "sha256": "2f1a...", "status": "done",
   "created_at": "...", "page_count": 1, "pages_needing_review": 0,
   "corrections_count": 2, "error": null,
   "result_url": "/jobs/af3c...", "receipt_url": "/jobs/af3c.../receipt"}
]}
```

`page_count`, `pages_needing_review`, `corrections_count` and `receipt_url` are `null`
until the job is `done`. `corrections_count` counts only saved corrections that STILL
APPLY to the current OCR word (same still-applies rule as export): stale entries whose
`before` no longer matches are not counted, and duplicates of the same word count once.

## GET /search

`?q=` (required), `?limit=` (1-100, default 20), `?offset=`. Whole-word tokens, implicit AND,
over the FINAL line text of `done` jobs: saved corrections are folded in when a job finishes
and the index refreshes on every `PUT /jobs/{id}/corrections`, so a corrected word (e.g.
`வாலறிவன்` after fixing `வாழறிவன்`) hits the corrected page, and the corrected-away word
stops hitting it. FTS5 `unicode61` tokenizer - Tamil letters tokenize as word characters.

```json
{"query": "வாலறிவன்", "total": 1, "limit": 20, "offset": 0, "results": [
  {"job_id": "af3c...", "filename": "scan.jpg", "page": 1, "line": "L3",
   "snippet": "...பயனென்கொல் <mark>வாலறிவன</mark>்", "score": -1.23}
]}
```

Results are bm25-ranked (lower `score` = better); hits are wrapped in `<mark>`.
Empty `q` → `400`; FTS unavailable in this SQLite build → `503`.

## Swagger

http://127.0.0.1:8000/docs → interactive Swagger UI generated by FastAPI; try `POST /jobs`
from the browser. The raw OpenAPI spec is at `/openapi.json`.

## Export + receipt (slice: pipe-export-receipt)

Only for jobs with `status: "done"`. Unknown job → `404`; job still `pending` or `error` → `409`;
master file gone from `uploads/` → `410` (PDF only). All three accept an optional `?reviewer=<name>`
that is written into the receipt (the backend does not store a reviewer yet).

| Method | Path | Returns |
|---|---|---|
| GET | `/jobs/{job_id}/export.pdf` | Searchable PDF (`application/pdf`, attachment). `?receipt_page=false` drops the last page |
| GET | `/jobs/{job_id}/export.txt` | UTF-8 text: `# ` provenance header, then `=== page N ===` blocks of line bodies in `seq` order |
| GET | `/jobs/{job_id}/receipt` | Processing receipt JSON (below) |

Both exports also send an `X-Master-SHA256` header.

**PDF:** each page is the 1600px-capped scan (same image the bboxes use, 150 dpi) with an invisible
Tamil text layer: one text object per line, stretched over its `bbox`, font = bundled
`fonts/noto-sans-tamil.ttf` (CID, ToUnicode) so Ctrl+F / `pdftotext` find the Tamil words. `[stub]`
lines are left out of the text layer. A visible receipt page is appended, and the Info dictionary
carries `Title`, `Keywords` (`master-sha256:<hash> job:<id>`) plus custom keys `PinkCloudJobId`,
`PinkCloudMasterSHA256`, `PinkCloudReceipt` (the receipt JSON).

**Receipt JSON:**

```json
{
  "receipt_version": 1,
  "job_id": "f9f1...", "filename": "scan.jpg", "status": "done",
  "master": {"sha256": "6b40...", "file": "master.jpg", "verified_on_disk": true},
  "page_count": 1,
  "pages": {"auto": 1, "human_review": 0},
  "lines": {"total": 4, "auto": 4, "human_review": 0},
  "corrections": {"total": 0, "by_tier": {}, "human_verdicts": 0},
  "reviewer": "Tinku",
  "time": {"created_at": "...", "exported_at": "...", "processing_ms_total": 4181},
  "ocr": {"engine_now": "paddle", "stub_pages": 0, "review_floor": 0.5},
  "per_page": [{"page": 1, "profile": "FAST", "needs_review": false, "lines": 4,
                "lines_auto": 4, "lines_human_review": 0, "corrections": 0, "processing_ms": 4181}]
}
```

- `pages.human_review` = pages with `needs_review: true`.
- A line counts as `human_review` if its confidence < 0.5, it is a `[stub]` line, or its page is `HEAVY`.
- `corrections` counts the page `corrections[]` (by `tier`); `human_verdicts` counts `verdicts[]` with
  `source: "review"`. Both are 0 until the editor saves edits back to the backend.
- `master.verified_on_disk` re-hashes the stored master at request time.
