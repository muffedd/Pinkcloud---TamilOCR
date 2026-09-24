"""GET /jobs reads stored summary columns (page_count, pages_needing_review,
corrections_count) instead of parsing every job's result JSON and reading
its corrections.json. These tests pin: the columns are maintained by
set_result and the corrections PUT, old databases are migrated and
backfilled on init, the list query uses the created_at index, and the
response shape is unchanged.

Run from fastapi/sqlite/:
    python -m pytest tests/test_jobs_list_summary.py -v
"""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db, export, storage
from app.main import app
from app.schema_out import build_job_result, build_page_result

RAW_WORD = "வாழறிவன்"
FIXED_WORD = "வாலறிவன்"
LINE = f"கற்றதனால் ஆய பயனென்கொல் {RAW_WORD}"
FIX = {"page": 1, "line": "L1", "word": 4, "before": RAW_WORD, "after": FIXED_WORD}
STALE = {"page": 1, "line": "L1", "word": 2, "before": "நீந்தற்", "after": "ஆய"}

LIST_KEYS = {
    "job_id", "filename", "sha256", "status", "created_at",
    "page_count", "pages_needing_review", "error",
    "result_url", "receipt_url", "corrections_count",
}


def _page(n, bodies, conf):
    lines = [{"body": b, "bbox": [10, 10 + i * 40, 500, 30], "confidence": conf}
             for i, b in enumerate(bodies)]
    return build_page_result(
        page_number=n, profile="FAST",
        quality={"blur": 1.0, "contrast": 1.0, "noise": 1.0, "skew_deg": 0.0},
        ocr_lines=lines, processing_ms=1.0)


def _result():
    """Two pages: page 1 clean (holds RAW_WORD), page 2 needs review."""
    pages = [_page(1, [LINE], 0.95), _page(2, ["x"], 0.1)]
    return json.dumps(build_job_result(pages), ensure_ascii=False)


def _row(job_id):
    con = sqlite3.connect(db.DB_PATH)
    try:
        return con.execute(
            "SELECT page_count, pages_needing_review, corrections_count"
            " FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        con.close()


@pytest.fixture()
def env(tmp_path):
    db.DB_PATH = tmp_path / "test.db"
    storage.UPLOAD_ROOT = tmp_path / "uploads"
    db.init_db()
    job_id = "a" * 32
    db.create_job(job_id, "kural.pdf", "9" * 64)
    db.set_result(job_id, "done", _result())
    with TestClient(app, raise_server_exceptions=True) as client:
        yield client, job_id


def _list_row(client, job_id):
    r = client.get("/jobs")
    assert r.status_code == 200, r.text
    return next(j for j in r.json()["jobs"] if j["job_id"] == job_id)


def _put(client, job_id, corrections):
    r = client.put(f"/jobs/{job_id}/corrections", json={"corrections": corrections})
    assert r.status_code == 200, r.text


def test_set_result_stores_counts(env):
    client, job_id = env
    expected_review = sum(
        1 for p in json.loads(_result())["pages"] if p.get("needs_review"))
    assert expected_review >= 1
    assert _row(job_id) == (2, expected_review, 0)
    row = _list_row(client, job_id)
    assert row["page_count"] == 2
    assert row["pages_needing_review"] == expected_review
    assert row["corrections_count"] == 0


def test_counts_update_after_corrections_put(env):
    client, job_id = env
    _put(client, job_id, [FIX])
    assert _row(job_id)[2] == 1
    assert _list_row(client, job_id)["corrections_count"] == 1
    # Stale entries do not count.
    _put(client, job_id, [FIX, STALE])
    assert _list_row(client, job_id)["corrections_count"] == 1
    # Clearing drops it back to zero.
    _put(client, job_id, [])
    assert _row(job_id)[2] == 0
    assert _list_row(client, job_id)["corrections_count"] == 0
    # Page counts untouched by corrections.
    assert _list_row(client, job_id)["page_count"] == 2


def test_put_still_refreshes_search(env):
    client, job_id = env
    _put(client, job_id, [FIX])
    assert client.get("/search", params={"q": FIXED_WORD}).json()["total"] == 1
    assert client.get("/search", params={"q": RAW_WORD}).json()["total"] == 0


def test_list_does_not_parse_results_or_read_corrections(env, monkeypatch):
    """GET /jobs must not apply corrections or count them per row."""
    client, job_id = env
    _put(client, job_id, [FIX])

    def boom(*a, **k):
        raise AssertionError("GET /jobs touched corrections")
    monkeypatch.setattr(export, "apply_saved_corrections", boom)
    monkeypatch.setattr(export, "count_applicable_corrections", boom)
    monkeypatch.setattr(export, "load_saved_corrections", boom)
    row = _list_row(client, job_id)
    assert row["corrections_count"] == 1
    assert row["page_count"] == 2


def test_list_query_skips_result_json_for_done_jobs(env):
    client, job_id = env
    err = "b" * 32
    db.create_job(err, "broken.pdf", "1" * 64)
    db.set_result(err, "error", json.dumps({"error": "boom"}))
    rows, total = db.list_jobs(50, 0)
    by_id = {r["id"]: r for r in rows}
    assert total == 2
    assert by_id[job_id]["result_json"] is None  # big blob never loaded
    assert json.loads(by_id[err]["result_json"])["error"] == "boom"
    api = _list_row(client, err)
    assert api["error"] == "boom"
    assert api["page_count"] is None
    assert api["pages_needing_review"] is None
    assert api["corrections_count"] is None


def test_unfinished_and_errored_jobs_have_null_counts(env):
    client, job_id = env
    pend = "c" * 32
    db.create_job(pend, "pending.pdf", "2" * 64)
    assert _row(pend) == (None, None, None)
    # A PUT on a pending job leaves its counts NULL.
    _put(client, pend, [FIX])
    assert _row(pend) == (None, None, None)
    assert _list_row(client, pend)["corrections_count"] is None
    # A done job re-run into error drops its counts.
    db.set_result(job_id, "error", json.dumps({"error": "rerun failed"}))
    assert _row(job_id) == (None, None, None)


def test_response_shape_identical(env):
    client, job_id = env
    body = client.get("/jobs").json()
    assert set(body) == {"total", "limit", "offset", "jobs"}
    assert set(body["jobs"][0]) == LIST_KEYS


def test_list_uses_created_at_index(env):
    con = sqlite3.connect(db.DB_PATH)
    try:
        idx = {r[1] for r in con.execute("PRAGMA index_list(jobs)")}
        assert "idx_jobs_created" in idx
        plan = " ".join(str(r[-1]) for r in con.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM jobs"
            " ORDER BY created_at DESC, id DESC LIMIT 50"))
        assert "idx_jobs_created" in plan
        assert "TEMP B-TREE" not in plan.upper()
    finally:
        con.close()


def test_init_migrates_and_backfills_old_database(tmp_path):
    """A pinkcloud.db from before the summary columns: init_db adds them,
    fills counts (incl. corrections already on disk) and the list works."""
    db.DB_PATH = tmp_path / "old.db"
    storage.UPLOAD_ROOT = tmp_path / "uploads"
    job_id, pend = "d" * 32, "e" * 32
    con = sqlite3.connect(db.DB_PATH)
    con.execute(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, filename TEXT NOT NULL,"
        " sha256 TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT,"
        " created_at TEXT NOT NULL)")
    con.execute("INSERT INTO jobs VALUES (?, 'old.pdf', ?, 'done', ?, ?)",
                (job_id, "3" * 64, _result(), "2026-09-01T00:00:00+00:00"))
    con.execute("INSERT INTO jobs VALUES (?, 'p.pdf', ?, 'pending', NULL, ?)",
                (pend, "4" * 64, "2026-09-02T00:00:00+00:00"))
    con.commit()
    con.close()
    cdir = storage.UPLOAD_ROOT / job_id
    cdir.mkdir(parents=True)
    (cdir / "corrections.json").write_text(
        json.dumps({"corrections": [FIX, STALE]}, ensure_ascii=False),
        encoding="utf-8")

    db.init_db()
    db.init_db()  # idempotent
    page_count, review, corr = _row(job_id)
    assert (page_count, corr) == (2, 1)
    assert review >= 1
    assert _row(pend) == (None, None, None)
    with TestClient(app, raise_server_exceptions=True) as client:
        rows = {j["job_id"]: j for j in client.get("/jobs").json()["jobs"]}
        assert rows[job_id]["corrections_count"] == 1
        assert rows[job_id]["page_count"] == 2
        assert rows[pend]["page_count"] is None
        assert set(rows[job_id]) == LIST_KEYS


def test_done_row_without_counts_is_filled_lazily(env):
    """Safety net: a done row written outside set_result still lists
    correct counts (computed once, then stored)."""
    client, job_id = env
    other = "f" * 32
    db.create_job(other, "raw.pdf", "5" * 64)
    con = sqlite3.connect(db.DB_PATH)
    con.execute("UPDATE jobs SET status = 'done', result_json = ? WHERE id = ?",
                (_result(), other))
    con.commit()
    con.close()
    assert _row(other) == (None, None, None)
    row = _list_row(client, other)
    assert row["page_count"] == 2
    assert row["corrections_count"] == 0
    assert _row(other)[0] == 2
