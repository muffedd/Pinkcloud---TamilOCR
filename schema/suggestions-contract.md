# Suggestions contract - proposed (S2 editor queue)

Status: PROPOSED. The editor (S2) reads this today from MOCK data; the
lexical suggestion source is not built yet. Additive and optional: pages
without the field behave exactly as before.

## Where it lives

A new optional **page-level** array `suggestions[]` on each
`result.pages[]` object, next to `corrections[]` and `verdicts[]`.

Not on the line objects: `$defs/line` has `additionalProperties: false`, so
a line-level field would fail validation for every existing consumer. Not
in `words[]` either: that array is optional, the fast pass does not emit it,
and `$defs/word` is closed too. Not in `corrections[]`: the receipt counts
those as applied corrections (`corrections.total`, `by_tier`), and `tier`
only allows T1/T2 - candidates would inflate the receipt.

Because the page object is also `additionalProperties: false`, the backend
must add `suggestions` to `schema/schema.json` in the same change that
starts emitting it (fragment below).

## Shape

```json
"suggestions": [
  {
    "line": "L3",
    "word": 4,
    "before": "வாழறிவன்",
    "candidates": [
      {"text": "வாலறிவன்", "score": 0.93, "source": "lexicon"},
      {"text": "வாளறிவன்", "score": 0.41, "source": "lexicon"}
    ]
  }
]
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `line` | string | yes | Line id, matches `lines[].id` (`"L3"`) |
| `word` | int >= 1 | yes | 1-based word index in that line's `body` split on whitespace - the same index `PUT /jobs/{id}/corrections` uses (`word`) |
| `before` | string | yes | The OCR word the candidates were computed for. If it no longer equals the word at `line`/`word`, the editor ignores the entry (stale) |
| `candidates` | array | yes | Ranked candidates, best first. Max 9 are shown (number keys 1-9) |
| `candidates[].text` | string | yes | Replacement word, Unicode Tamil |
| `candidates[].score` | number 0..1 | no | Ranking score from the source. The editor sorts by it (entries without a score go last) and shows it |
| `candidates[].source` | string | no | Where it came from: `lexicon`, `repair`, `rules`, `llm` (free string; shown as a label) |

Editor rules: duplicates and candidates equal to the OCR word are dropped. A
page `corrections[]` entry whose `before` matches the word becomes candidate
1 (label = its tier's provenance). Picking a candidate (number key or click)
records a normal human correction via `PUT /jobs/{id}/corrections`
(`{page, line, word, before, after}`) - nothing new on the save path.

## schema.json fragment

```json
"suggestions": {
  "type": "array",
  "description": "Optional ranked replacement candidates for doubtful words (review queue).",
  "items": {"$ref": "#/$defs/suggestion"}
}
```

```json
"suggestion": {
  "type": "object",
  "additionalProperties": false,
  "required": ["line", "word", "before", "candidates"],
  "properties": {
    "line": {"type": "string"},
    "word": {"type": "integer", "minimum": 1},
    "before": {"type": "string"},
    "candidates": {
      "type": "array",
      "maxItems": 9,
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["text"],
        "properties": {
          "text": {"type": "string"},
          "score": {"$ref": "#/$defs/confidence"},
          "source": {"type": "string"}
        }
      }
    }
  }
}
```

## Mock data today

- `editor.html?mock=1` - Kural demo page + `MOCK_SUGGESTIONS` in editor.js.
- `editor.html?mock=1&fixture=sample1` (or `sample2`) - real Sarvam sample
  output on its real scan (`raw/<name>.png`), built by
  `samples/editor/make_fixtures.py`. Its candidates are fake (`source: "mock"`,
  not real Tamil corrections) and five sample1 line
  confidences are forced low so the queue has work; see the script header.
