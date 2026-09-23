# AI fix + learned-fix dictionary - proposed (V4 editor)

Status: PROPOSED. The editor ships against these shapes. `POST /suggest` is
answered by a local stub until the backend route lands (flip
`AI_SUGGEST_LIVE = true` in api.js). `GET /dictionary` and
`POST /dictionary/skip` are called now and fall back silently when missing.

## POST /jobs/{job_id}/suggest

Asks the LLM (gemini-3.1-flash-lite) for a reading of one Doubt word.

Request:
```json
{"page": 1, "line": "L3", "word": 3, "before": "ாமுதலிய", "context": "<full line body>"}
```
`word` is the 1-based index used by `PUT /jobs/{id}/corrections`.

Response 200:
```json
{"candidates": [{"text": "முதலிய", "score": 0.88, "source": "llm"}]}
```
The editor shows the first candidate as suggestion 1 with an "AI" label (no
score is shown: it is not a recognition probability). Candidates equal to
`before` are dropped. An empty list gives the toast "AI found no better
reading". Accepting a candidate saves a normal human correction via
`PUT /jobs/{id}/corrections`. Nothing new is added to the save path.

Errors: any non-2xx (503 = model unavailable) or network failure gives one
"AI unavailable" toast. The AI fix button then stays hidden for the browser
session (`sessionStorage pc.aioff`). The button only shows on unfixed Doubt
words while the connection badge reads Connected.

## GET /dictionary

The learned OCR word -> fix pairs shared across jobs (the flywheel).
```json
{"entries": [{"before": "Aiyar", "after": "அய்யர்"}]}
```
(`{"entries": {"Aiyar": "அய்யர்"}}` is accepted too.) Server entries override
the local `pc.fixdict` cache, and local-only pairs still apply. On 404,
network failure, any error, or no answer within 2.5s, the editor uses
`pc.fixdict` alone and says nothing. A fix applied from the dictionary gets
the provenance label "learned from your earlier fixes".

## POST /dictionary/skip

Sent fire-and-forget when a reviewer undoes a learned fix:
```json
{"job_id": "…", "page": 1, "line": "L1", "word": 3, "before": "Aiyar", "after": "அய்யர்"}
```
The per-job local skip list (`pc.dictskips.<job>`) stays the fallback. The
response is ignored.
