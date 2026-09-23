# AI fix + learned-fix dictionary (V4)

Status: SHIPPED on the backend. `GET /dictionary` and `POST /dictionary/skip`
come from the flywheel (`app/flywheel.py`). `POST /jobs/{job_id}/suggest` is
live in `app/suggest.py`, but it answers 503 until `GEMINI_API_KEY` is set on
the server. The editor still answers suggest from its local stub until
`AI_SUGGEST_LIVE = true` in api.js.

## POST /jobs/{job_id}/suggest

Asks the LLM for up to 3 readings of one Doubt word.

Model: Gemini API, `gemini-3.1-flash-lite` (stable model code per
https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite; the older
3.1 Flash-Lite Preview id is shut down). Override with `SUGGEST_MODEL`.

Server environment:

| Variable | Effect |
| --- | --- |
| `GEMINI_API_KEY` | Turns AI fix on. Read at call time, sent only as the `x-goog-api-key` header, never logged or returned. |
| `SUGGEST_PROVIDER` | Unset or `gemini` = Gemini. `off` = disabled even with a key. `sarvam` = Sarvam chat (`sarvam-105b`, uses `SARVAM_API_KEY`), explicit opt-in only. |
| `SUGGEST_MODEL` | Model id override. |
| `SUGGEST_TIMEOUT_S` | Per-request budget, default 8. |

Request:
```json
{"page": 1, "line": "L3", "word": 3, "before": "ாமுதலிய", "context": "<full line body>"}
```
`word` is the 1-based index used by `PUT /jobs/{id}/corrections`. `before` is
required (1-256 chars after trimming). The server reads the target line and 2
lines on each side from the stored job (with saved corrections applied).
`context` is only used when the line id is not found on that page.

Response 200:
```json
{"candidates": [{"text": "முதலிய", "score": 0.88, "source": "llm"}]}
```
At most 3 candidates, best first. The server trims them, drops blanks,
duplicates and anything equal to `before`, and clamps `score` to 0..1. The
score is the model's own guess, not a recognition probability, so the editor
does not show it. An empty list means "AI found no better reading".
Accepting a candidate saves a normal human correction via
`PUT /jobs/{id}/corrections` (which also teaches the dictionary below).

Errors:
- 404 unknown job, page, or line (line only when no `context` is sent)
- 409 job not done yet
- 422 bad body
- 503 `{"detail": "AI suggestions unavailable"}`: no key, `SUGGEST_PROVIDER=off`,
  timeout, provider error, or an unreadable model reply. No provider details
  are ever passed through.

The editor treats any non-2xx or network failure as one "AI unavailable"
toast and hides the AI fix button for the session (`sessionStorage pc.aioff`).
The button only shows on unfixed Doubt words while the connection badge
reads Connected. The first candidate is shown as suggestion 1 with an "AI"
label.

`GET /health` now also reports `ai_suggest` (bool) and `ai_suggest_provider`
(`"gemini"`, `"sarvam"` or null). It never reports the key.

## GET /dictionary

The learned OCR word -> fix pairs shared across jobs (the flywheel).
```json
{"dictionary": [{"before": "Aiyar", "after": "அய்யர்", "count": 3}], "min_accepts": 2}
```
`?limit=` 1-1000, default 200. A pair is served once it was accepted in at
least 2 corrections PUTs and more often accepted than skipped, strongest
first. The editor reads `entries` or `dictionary`, so this shape works as is.
Server entries override the local `pc.fixdict` cache, and local-only pairs
still apply. On 404, network failure, any error, or no answer within 2.5s,
the editor uses `pc.fixdict` alone and says nothing. A fix applied from the
dictionary gets the label "learned from your earlier fixes".

Storage: local `flywheel.db` next to `pinkcloud.db` by default. Turso (libsql
embedded replica) only when `LIBSQL_URL` and `LIBSQL_AUTH_TOKEN` are both set
and the `libsql` package is installed.

## POST /dictionary/skip

Sent fire-and-forget when a reviewer undoes a learned fix:
```json
{"job_id": "…", "page": 1, "line": "L1", "word": 3, "before": "Aiyar", "after": "அய்யர்"}
```
The server uses `before` and `after` (both required, 1-256 chars) and ignores
the other fields. Response: `{"before": …, "after": …, "skipped": true}`.
The pair stops being served once its skips catch up with its accepts. The
per-job local skip list (`pc.dictskips.<job>`) stays the fallback.
