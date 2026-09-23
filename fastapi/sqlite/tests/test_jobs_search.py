"""Tests for GET /jobs (list) and GET /search (FTS over OCR text).

Run from fastapi/sqlite/:
    python -m pytest tests/test_jobs_search.py -v
"""

import json
import sqlite3

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import db, storage
from app.main import app
from app.schema_out import build_job_result, build_page_result


def _lines(*bodies, conf=0.9):
    return [
        {"body": b, "bbox": [10, 10 + i * 40, 500, 30], "confidence": conf}
        for i, b in enumerate(bodies)
    ]


def _result(*page_specs):
    """page_specs: (page_number, lines) tuples -> contract result JSON."""
    pages = [
        build_page_result(
            page_number=no,
            profile="FAST",
            quality={"blur": 1.0, "contrast": 1.0, "noise": 1.0, "skew_deg": 0.0},
            ocr_lines=lines,
            processing_ms=1.0,
        )
        for no, lines in page_specs
    ]
    return json.dumps(build_job_result(pages), ensure_ascii=False)


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """Client + four seeded jobs: two done, one pending, one error."""
    tmp = tmp_path_factory.mktemp("jobsearch")
    db.DB_PATH = tmp / "test.db"
    storage.UPLOAD_ROOT = tmp / "uploads"
    db.init_db()

    a = "a" * 32
    db.create_job(a, "narrinai.pdf", "0" * 64)
    db.set_result(a, "done", _result(
        (1, _lines("தமிழ் மொழி அழகிய மொழி", "கல்வி சிறந்த ஒளி")),
        (2, _lines("another தமிழ் line second page", conf=0.1)),  # low conf -> needs_review
    ))

    b = "b" * 32
    db.create_job(b, "paripuranam.pdf", "1" * 64)
    db.set_result(b, "done", _result(
        (1, _lines("பாடல் வரிகள் மட்டும்", "தமிழ் பாடல் ஒன்று")),
    ))

    c = "c" * 32
    db.create_job(c, "pending.pdf", "2" * 64)  # stays pending

    d = "d" * 32
    db.create_job(d, "broken.pdf", "3" * 64)
    db.set_result(d, "error", json.dumps({"error": "boom"}))

    with TestClient(app, raise_server_exceptions=True) as client:
        yield client, {"a": a, "b": b, "c": c, "d": d}


# --------------------------------------------------------------------------
# GET /jobs
# --------------------------------------------------------------------------

def test_list_jobs_shape_and_fields(seeded):
    client, ids = seeded
    r = client.get("/jobs")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 4
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["jobs"]) == 4

    by_id = {j["job_id"]: j for j in body["jobs"]}
    assert set(by_id) == set(ids.values())

    done_a = by_id[ids["a"]]
    assert done_a["filename"] == "narrinai.pdf"
    assert done_a["sha256"] == "0" * 64
    assert done_a["status"] == "done"
    assert done_a["created_at"]
    assert done_a["page_count"] == 2
    assert done_a["pages_needing_review"] == 1
    assert done_a["error"] is None
    assert done_a["result_url"] == f"/jobs/{ids['a']}"
    assert done_a["receipt_url"] == f"/jobs/{ids['a']}/receipt"

    pending = by_id[ids["c"]]
    assert pending["status"] == "pending"
    assert pending["page_count"] is None
    assert pending["pages_needing_review"] is None
    assert pending["receipt_url"] is None
    assert pending["result_url"] == f"/jobs/{ids['c']}"

    errored = by_id[ids["d"]]
    assert errored["status"] == "error"
    assert errored["error"] == "boom"
    assert errored["page_count"] is None
    assert errored["receipt_url"] is None


def test_list_jobs_newest_first(seeded):
    client, ids = seeded
    jobs = client.get("/jobs").json()["jobs"]
    created = [j["created_at"] for j in jobs]
    assert created == sorted(created, reverse=True)


def test_list_jobs_pagination(seeded):
    client, _ = seeded
    page1 = client.get("/jobs", params={"limit": 2}).json()
    assert len(page1["jobs"]) == 2 and page1["total"] == 4
    page2 = client.get("/jobs", params={"limit": 2, "offset": 2}).json()
    assert len(page2["jobs"]) == 2 and page2["offset"] == 2
    ids1 = {j["job_id"] for j in page1["jobs"]}
    ids2 = {j["job_id"] for j in page2["jobs"]}
    assert ids1.isdisjoint(ids2)
    assert client.get("/jobs", params={"limit": 3, "offset": 4}).json()["jobs"] == []


def test_list_jobs_bad_params_rejected(seeded):
    client, _ = seeded
    assert client.get("/jobs", params={"limit": 0}).status_code == 422
    assert client.get("/jobs", params={"limit": 201}).status_code == 422
    assert client.get("/jobs", params={"offset": -1}).status_code == 422


# --------------------------------------------------------------------------
# GET /search
# --------------------------------------------------------------------------

def test_search_tamil_term_hits_with_refs_and_snippet(seeded):
    client, ids = seeded
    r = client.get("/search", params={"q": "தமிழ்"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["query"] == "தமிழ்"
    assert body["total"] == 3  # A p1 L1, A p2 L1, B p1 L2
    assert len(body["results"]) == 3

    refs = {(x["job_id"], x["page"], x["line"]) for x in body["results"]}
    assert (ids["a"], 1, "L1") in refs
    assert (ids["a"], 2, "L1") in refs
    assert (ids["b"], 1, "L2") in refs
    for x in body["results"]:
        # unicode61 splits the pulli (்) off the token, so the mark wraps
        # "தமிழ" while the query "தமிழ்" still matches it.
        assert "<mark>தமிழ" in x["snippet"]
        assert x["filename"].endswith(".pdf")
        assert isinstance(x["score"], float)


def test_search_multiple_tokens_are_anded(seeded):
    client, ids = seeded
    body = client.get("/search", params={"q": "தமிழ் பாடல்"}).json()
    assert body["total"] == 1
    hit = body["results"][0]
    assert (hit["job_id"], hit["page"], hit["line"]) == (ids["b"], 1, "L2")


def test_search_english_token_mixed_script(seeded):
    client, ids = seeded
    body = client.get("/search", params={"q": "another"}).json()
    assert body["total"] == 1
    assert body["results"][0]["job_id"] == ids["a"]
    assert body["results"][0]["page"] == 2


def test_search_no_match(seeded):
    client, _ = seeded
    body = client.get("/search", params={"q": "இல்லாதசொல்"}).json()
    assert body["total"] == 0
    assert body["results"] == []


def test_search_fts_operators_in_input_are_safe(seeded):
    """Raw FTS5 syntax in the query must not error out (it is quoted)."""
    client, _ = seeded
    for q in ['" OR NEAR(', "AND * (", 'தமிழ் OR "']:
        r = client.get("/search", params={"q": q})
        assert r.status_code == 200, (q, r.text)


def test_search_empty_or_missing_query(seeded):
    client, _ = seeded
    assert client.get("/search").status_code == 422          # q required
    assert client.get("/search", params={"q": ""}).status_code == 422
    assert client.get("/search", params={"q": "   "}).status_code == 400


def test_search_pagination(seeded):
    client, _ = seeded
    p1 = client.get("/search", params={"q": "தமிழ்", "limit": 2}).json()
    assert len(p1["results"]) == 2 and p1["total"] == 3
    p2 = client.get("/search", params={"q": "தமிழ்", "limit": 2, "offset": 2}).json()
    assert len(p2["results"]) == 1 and p2["total"] == 3


def test_search_excludes_pending_and_error_jobs(seeded):
    """Only indexed (done) jobs can ever match; c and d have no OCR text."""
    client, ids = seeded
    body = client.get("/search", params={"q": "boom"}).json()
    assert body["total"] == 0  # error payloads are not indexed


def test_reindex_replaces_stale_text(seeded):
    """set_result re-indexes: old terms disappear, new terms appear."""
    client, _ = seeded
    job = "e" * 32
    db.create_job(job, "redo.pdf", "4" * 64)
    db.set_result(job, "done", _result((1, _lines("பழைய உரை மட்டும்"))))
    assert client.get("/search", params={"q": "பழைய"}).json()["total"] == 1
    db.set_result(job, "done", _result((1, _lines("புதிய உரை மட்டும்"))))
    assert client.get("/search", params={"q": "பழைய"}).json()["total"] == 0
    assert client.get("/search", params={"q": "புதிய"}).json()["total"] == 1


def test_startup_reconcile_indexes_pre_feature_jobs(seeded):
    """A done job inserted straight into SQL (no set_result indexing)
    becomes searchable after init_db() reconciles the index."""
    client, _ = seeded
    job = "f" * 32
    legacy = _result((1, _lines("பண்டைய குறிப்பு உரை")))
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO jobs (id, filename, sha256, status, result_json, created_at)"
            " VALUES (?, 'legacy.pdf', ?, 'done', ?, '2026-01-01T00:00:00+00:00')",
            (job, "5" * 64, legacy),
        )
    assert client.get("/search", params={"q": "பண்டைய"}).json()["total"] == 0
    db.init_db()  # startup reconcile
    body = client.get("/search", params={"q": "பண்டைய"}).json()
    assert body["total"] == 1
    assert body["results"][0]["job_id"] == job


# --------------------------------------------------------------------------
# end-to-end: upload -> listed -> searchable (stub OCR is fine here)
# --------------------------------------------------------------------------

def test_uploaded_job_is_listed_and_searchable(tmp_path):
    db.DB_PATH = tmp_path / "e2e.db"
    storage.UPLOAD_ROOT = tmp_path / "uploads"
    db.init_db()

    img = np.full((800, 600), 235, np.uint8)
    cv2.putText(img, "TAMIL TEXT LINE", (20, 400),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, 20, 2)
    png = tmp_path / "page.png"
    cv2.imwrite(str(png), img)

    with TestClient(app, raise_server_exceptions=True) as client:
        with open(png, "rb") as f:
            r = client.post("/jobs", files={"file": ("page.png", f, "image/png")})
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]

        listing = client.get("/jobs").json()
        assert listing["total"] == 1
        job = listing["jobs"][0]
        assert job["job_id"] == job_id
        assert job["status"] == "done"
        assert job["page_count"] == 1

        # stub OCR lines are clearly marked "[stub]" and ARE indexed
        hits = client.get("/search", params={"q": "stub"}).json()
        assert hits["total"] >= 1
        assert hits["results"][0]["job_id"] == job_id
