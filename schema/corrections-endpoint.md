# Corrections endpoint - implementation spec (for Tinku)

Reviewers fix doubtful OCR words in the editor; those fixes must persist so
export and future scans can use them. This adds one small resource under the
existing job. Style and error shape match `schema/endpoints.md` (app 0.2.0).

| Method | Path | What it does |
|---|---|---|
| PUT | `/jobs/{job_id}/corrections` | Replace the job's full correction map |
| GET | `/jobs/{job_id}/corrections` | Return the job's current correction map |

The editor sends the **full map every time** (debounced after each accepted
fix), so PUT is a wholesale replace - no patch/merge logic, no per-word
routes. State stays tiny: one JSON document per job.

## Data model

One correction per reviewer-fixed word. Word refs mirror `result.pages`
structure: `lines[].id` plus the 1-based word index you get by splitting the
line `body` on whitespace (the same derivation the editor uses for word keys
`p<page>:<line>:w<word>`).

| Field | Type | Notes |
|---|---|---|
| `page` | int | 1-based page number, matches `result.pages[].page` |
| `line` | string | Line id, e.g. `"L3"`, matches `result.pages[].lines[].id` |
| `word` | int | 1-based index of the word within the line body (split on whitespace) |
| `before` | string | OCR word as originally recognized |
| `after` | string | Reviewer-accepted replacement (Unicode Tamil) |

## PUT /jobs/{job_id}/corrections

```bash
curl -s -X PUT http://127.0.0.1:8000/jobs/af3c8e14.../corrections \
  -H "Content-Type: application/json" \
  -d '{"corrections": [
        {"page": 1, "line": "L3", "word": 5, "before": "வாழறிவன்", "after": "வாலறிவன்"}
      ]}'
# → {"job_id": "af3c8e14...", "corrections": [...], "updated_at": "2026-09-23T12:00:00+00:00"}
```

Semantics: **replace**. Store the array as given (order is reading order but
not meaningful). An empty array clears the job's corrections.

| Status | Body | When |
|---|---|---|
| `200` | `{"job_id", "corrections", "updated_at"}` | Stored |
| `404` | `{"detail": "job not found"}` | Unknown `job_id` |
| `422` | FastAPI validation error | Bad payload shape |

## GET /jobs/{job_id}/corrections

```bash
curl -s http://127.0.0.1:8000/jobs/af3c8e14.../corrections
# → {"job_id": "af3c8e14...", "corrections": [...], "updated_at": "..."} 
```

| Status | Body | When |
|---|---|---|
| `200` | Job's corrections (`corrections: []` and `updated_at: null` when none saved yet) | Job exists |
| `404` | `{"detail": "job not found"}` | Unknown `job_id` |

Note: the editor treats **any 404 from this route as "not deployed yet"** and
falls back to localStorage silently - so ship both methods together, and only
404 for a genuinely unknown job.

## Implementation notes (minutes, not hours)

- Storage: one JSON file per job next to the upload (`uploads/<job_id>/corrections.json`)
  or a `corrections(job_id, doc_json, updated_at)` SQLite table - either fits
  the current storage layer. Full-document read/write, no queries by word.
- Pydantic model: `Correction{page:int, line:str, word:int, before:str, after:str}`,
  `CorrectionsDoc{corrections: list[Correction]}`.
- **Export must apply them**: wherever `export.pdf` (or any export) builds text
  from `result.pages`, substitute `after` for the word at (page, line, word)
  before rendering. Until then, export uses raw OCR.
- No auth, size cap, or history for now - same trust level as the rest of the
  local API. Suggest a soft cap (~10k corrections/job) to bound the JSON blob.

## Frontend behavior already built against this

- Save: debounced PUT of the full map after every accepted fix.
- Load: GET on editor open; corrections reapply to the text layer (prov = human).
- Fallback: 404 -> corrections live in localStorage keyed by job id; export
  then applies them client-side to the text instead of opening the raw PDF.
