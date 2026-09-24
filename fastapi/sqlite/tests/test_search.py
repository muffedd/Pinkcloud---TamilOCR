"""Tests for FINAL-text search (slice 3): the FTS index holds corrected
line text, refreshed when a job finishes and on every corrections PUT, and
GET /jobs rows count only still-applicable corrections.

Run from fastapi/sqlite/:
    python -m pytest tests/test_search.py -v
"""

import json

import pytest
from fastapi.testclient import TestClient

from app import db, storage
from app.main import app
from app.schema_out import build_job_result, build_page_result

RAW_WORD = "வாழறிவன்"
FIXED_WORD = "வாலறிவன்"
LINE = f"கற்றதனால் ஆய பயனென்கொல் {RAW_WORD}"  # RAW_WORD is word 4
FIX = {"page": 1, "line": "L1", "word": 4, "before": RAW_WORD, "after": FIXED_WORD}


def _result(*bodies, conf=0.9):
    lines = [
        {"body": b, "bbox": [10, 10 + i * 40, 500, 30], "confidence": conf}
        for i, b in enumerate(bodies)
    ]
    page = build_page_result(
        page_number=1,
        profile="FAST",
        quality={"blur": 1.0, "contrast": 1.0, "noise": 1.0, "skew_deg": 0.0},
        ocr_lines=lines,
        processing_ms=1.0,
    )
    return json.dumps(build_job_result([page]), ensure_ascii=False)


@pytest.fixture()
def job(tmp_path):
    """Isolated DB + upload root, one done job containing RAW_WORD."""
    db.DB_PATH = tmp_path / "test.db"
    storage.UPLOAD_ROOT = tmp_path / "uploads"
    db.init_db()
    job_id = "e" * 32
    db.create_job(job_id, "kural.pdf", "9" * 64)
    db.set_result(job_id, "done", _result(LINE, "அகர முதல எழுத்தெல்லாம் ஆதி"))
    with TestClient(app, raise_server_exceptions=True) as client:
        yield client, job_id


def _hits(client, q):
    r = client.get("/search", params={"q": q})
    assert r.status_code == 200, r.text
    return r.json()


def _put(client, job_id, corrections):
    r = client.put(f"/jobs/{job_id}/corrections",
                   json={"corrections": corrections})
    assert r.status_code == 200, r.text


def test_raw_text_searchable_before_any_correction(job):
    client, job_id = job
    assert _hits(client, RAW_WORD)["total"] == 1
    assert _hits(client, FIXED_WORD)["total"] == 0


def test_correction_makes_fixed_word_searchable_and_raw_gone(job):
    """PUT corrections re-indexes: the saved fix is what search finds."""
    client, job_id = job
    _put(client, job_id, [FIX])

    fixed = _hits(client, FIXED_WORD)
    assert fixed["total"] == 1
    hit = fixed["results"][0]
    assert hit["job_id"] == job_id
    assert hit["page"] == 1
    assert hit["line"] == "L1"
    # FTS5's unicode61 tokenizer splits at the pulli (virama), so the
    # <mark> wraps the sub-token; matching itself works on the Tamil stem.
    assert "<mark>" in hit["snippet"] and FIXED_WORD[: -1] in hit["snippet"]

    # The corrected-away raw word must no longer hit the fixed line.
    assert _hits(client, RAW_WORD)["total"] == 0


def test_clearing_corrections_restores_raw_text(job):
    client, job_id = job
    _put(client, job_id, [FIX])
    assert _hits(client, FIXED_WORD)["total"] == 1
    _put(client, job_id, [])
    assert _hits(client, FIXED_WORD)["total"] == 0
    assert _hits(client, RAW_WORD)["total"] == 1


def test_stale_correction_changes_nothing(job):
    """A correction whose before-word no longer matches is skipped."""
    client, job_id = job
    stale = {**FIX, "before": "நீந்தற்", "after": "கற்றதனால்"}
    _put(client, job_id, [stale])
    assert _hits(client, RAW_WORD)["total"] == 1
    assert _hits(client, "கற்றதனால்")["total"] == 1  # the word was already there


def test_finish_indexes_corrected_text(tmp_path):
    """Corrections already on disk when a job FINISHES are indexed too."""
    db.DB_PATH = tmp_path / "test.db"
    storage.UPLOAD_ROOT = tmp_path / "uploads"
    db.init_db()
    job_id = "f" * 32
    db.create_job(job_id, "late.pdf", "8" * 64)
    # A saved correction that predates the result (e.g. a re-run job).
    corr_dir = storage.UPLOAD_ROOT / job_id
    corr_dir.mkdir(parents=True)
    (corr_dir / "corrections.json").write_text(
        json.dumps({"corrections": [FIX], "updated_at": "2026-09-24T00:00:00+00:00"},
                   ensure_ascii=False),
        encoding="utf-8")
    db.set_result(job_id, "done", _result(LINE))

    with TestClient(app, raise_server_exceptions=True) as client:
        assert _hits(client, FIXED_WORD)["total"] == 1
        assert _hits(client, RAW_WORD)["total"] == 0


def test_corrections_count_in_jobs_list(job):
    """corrections_count is additive and counts only still-applies fixes."""
    client, job_id = job
    def row():
        r = client.get("/jobs")
        assert r.status_code == 200, r.text
        return next(j for j in r.json()["jobs"] if j["job_id"] == job_id)

    assert row()["corrections_count"] == 0

    _put(client, job_id, [FIX])
    assert row()["corrections_count"] == 1

    # Stale entries do not inflate the count.
    stale = {"page": 1, "line": "L1", "word": 2,
             "before": "நீந்தற்", "after": "ஆய"}
    _put(client, job_id, [FIX, stale])
    assert row()["corrections_count"] == 1

    # Duplicate fixes for the same word dedupe to one (last one wins).
    _put(client, job_id, [FIX, dict(FIX)])
    assert row()["corrections_count"] == 1

    # A chained fix whose before is the first fix's after no longer matches
    # the raw OCR word, so it is stale under the still-applies rule - same
    # behaviour as export.txt/pdf (apply_corrections skips it there too).
    again = {**FIX, "before": FIXED_WORD, "after": "வாளறிவன்"}
    _put(client, job_id, [FIX, again])
    assert row()["corrections_count"] == 0

    # Clearing drops the count back to zero.
    _put(client, job_id, [])
    assert row()["corrections_count"] == 0


def test_corrections_count_null_for_unfinished_jobs(job):
    client, job_id = job
    pending = "1" * 32
    db.create_job(pending, "pending.pdf", "7" * 64)
    rows = {j["job_id"]: j for j in client.get("/jobs").json()["jobs"]}
    assert rows[pending]["corrections_count"] is None
    assert rows[job_id]["corrections_count"] == 0


def test_response_shapes_unchanged_plus_additive_key(job):
    """The Library page's contract: old keys keep their names and meaning;
    corrections_count is the only addition."""
    client, job_id = job
    r = client.get("/search", params={"q": "அகர"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"query", "total", "limit", "offset", "results"}
    assert set(body["results"][0]) == {
        "job_id", "filename", "page", "line", "snippet", "score"}

    job_row = next(j for j in client.get("/jobs").json()["jobs"]
                   if j["job_id"] == job_id)
    assert set(job_row) == {
        "job_id", "filename", "sha256", "status", "created_at",
        "page_count", "pages_needing_review", "error",
        "result_url", "receipt_url",
        "corrections_count",  # the additive slice-3 key
        "mode",  # additive: OCR mode chosen at upload (auto|light|heavy)
    }
