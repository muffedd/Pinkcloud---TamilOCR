"""Corrections flywheel: cross-job dictionary learned from corrections PUTs.

Covers: put/skip tallies, the suggestion gate (accepts >= 2 AND
accepts > skips), the zero-config local-sqlite fallback (no libsql
package / no Turso env vars), the raw_corrections fine-tuning log, and
the PUT fold-in diff (only newly accepted pairs teach the dictionary).

Run from fastapi/sqlite/:
    python -m pytest tests/test_flywheel.py -v
"""

import hashlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db, flywheel, storage
from app.main import app

import cv2
import numpy as np

FIX = {"page": 1, "line": "L3", "word": 5,
       "before": "வாழறிவன்", "after": "வாலறிவன்"}


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    """A flywheel store in a tmp dir, forced into local fallback mode."""
    tmp = tmp_path_factory.mktemp("flywheel-store")
    flywheel.DB_PATH = tmp / "flywheel.db"
    for var in ("LIBSQL_URL", "LIBSQL_AUTH_TOKEN"):
        pytest.MonkeyPatch().delenv(var, raising=False)
    flywheel.init_db()
    return flywheel


def _counts(store, before):
    conn = sqlite3.connect(store.DB_PATH)
    row = conn.execute(
        "SELECT after, accepts, skips FROM corrections_dict WHERE before = ?",
        (before,)).fetchone()
    conn.close()
    return row


def test_put_upserts_and_counts(store):
    store.put("aa", "bb", job_id="j1", page=1, line="L1")
    store.put("aa", "bb", job_id="j2", page=1, line="L1")
    assert _counts(store, "aa") == ("bb", 2, 0)
    # A later accepted replacement tracks the latest 'after'.
    store.put("aa", "cc", job_id="j3", page=2, line="L4")
    assert _counts(store, "aa") == ("cc", 3, 0)


def test_skip_counts_and_never_creates_suggestion(store):
    store.skip("dd", "ee")
    assert _counts(store, "dd") == ("ee", 0, 1)
    assert store.dictionary() == [] or all(
        d["before"] != "dd" for d in store.dictionary())


def test_suggestion_gate_accepts_and_skips(store):
    # accepts < MIN_ACCEPTS -> hidden
    store.put("gg", "hh")
    assert all(d["before"] != "gg" for d in store.dictionary())
    # accepts >= 2 and accepts > skips -> shown
    store.put("gg", "hh")
    assert any(d["before"] == "gg" and d["after"] == "hh" and d["count"] == 2
               for d in store.dictionary())
    # skips catching up -> hidden again
    store.skip("gg", "hh")
    store.skip("gg", "hh")
    assert all(d["before"] != "gg" for d in store.dictionary())
    # one more accept wins it back
    store.put("gg", "hh")
    assert any(d["before"] == "gg" and d["count"] == 3
               for d in store.dictionary())


def test_fallback_mode_without_turso_env(store, monkeypatch):
    monkeypatch.delenv("LIBSQL_URL", raising=False)
    monkeypatch.delenv("LIBSQL_AUTH_TOKEN", raising=False)
    assert store.using_libsql() is False
    # Even with env vars set, a missing libsql package falls back.
    monkeypatch.setenv("LIBSQL_URL", "libsql://example.turso.io")
    monkeypatch.setenv("LIBSQL_AUTH_TOKEN", "token")
    monkeypatch.setattr(store, "_libsql", None)
    assert store.using_libsql() is False
    monkeypatch.undo()
    assert store.using_libsql() is False  # still no env vars in tests


def test_raw_corrections_log(store):
    before = len(sqlite3.connect(store.DB_PATH).execute(
        "SELECT id FROM raw_corrections").fetchall())
    store.put("rr", "ss", job_id="job-9", page=3, line="L7")
    rows = sqlite3.connect(store.DB_PATH).execute(
        "SELECT before, after, job_id, page, line, created_at"
        " FROM raw_corrections").fetchall()
    assert len(rows) == before + 1
    assert rows[-1][:5] == ("rr", "ss", "job-9", 3, "L7")
    assert rows[-1][5]  # created_at filled


def test_learn_from_put_diff_only(store):
    old = [{"before": "x1", "after": "y1"}]
    new = [{"before": "x1", "after": "y1"},          # unchanged -> no learn
           {"before": "x2", "after": "y2", "page": 1, "line": "L2"}]
    assert store.learn_from_put("jobA", old, new) == 1
    assert _counts(store, "x2") == ("y2", 1, 0)
    # Re-saving the identical map teaches nothing (idempotent).
    assert store.learn_from_put("jobA", new, new) == 0
    assert _counts(store, "x2") == ("y2", 1, 0)
    # A pair accepted in ANOTHER job's PUT counts again (cross-job).
    assert store.learn_from_put("jobB", [], [new[1]]) == 1
    assert _counts(store, "x2") == ("y2", 2, 0)


# --- API: PUT fold-in, GET /dictionary, POST /dictionary/skip --------------

@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("flywheel-api")
    db.DB_PATH = tmp / "test.db"
    storage.UPLOAD_ROOT = tmp / "uploads"
    flywheel.DB_PATH = tmp / "flywheel.db"
    for var in ("LIBSQL_URL", "LIBSQL_AUTH_TOKEN"):
        pytest.MonkeyPatch().delenv(var, raising=False)
    db.init_db()
    with TestClient(app) as c:
        yield c


def _new_job(client, tmp_path):
    img = np.full((400, 600, 3), 235, np.uint8)
    cv2.putText(img, "TAMIL", (50, 200), cv2.FONT_HERSHEY_SIMPLEX, 2,
                (20, 20, 20), 3)
    ok, buf = cv2.imencode(".png", img)
    data = buf.tobytes()
    r = client.post("/jobs", files={"file": ("page.png", data, "image/png")})
    assert r.status_code == 200
    return r.json()["job_id"]


def test_put_foldin_dictionary_and_skip(client, tmp_path):
    pair = dict(FIX)
    job1 = _new_job(client, tmp_path)
    r = client.put(f"/jobs/{job1}/corrections", json={"corrections": [pair]})
    assert r.status_code == 200
    # One acceptance: learned (raw log) but not yet suggested.
    assert client.get("/dictionary").json()["dictionary"] == []

    # Re-PUT the same map: the diff is empty, accepts stays 1.
    r = client.put(f"/jobs/{job1}/corrections", json={"corrections": [pair]})
    assert r.status_code == 200
    assert client.get("/dictionary").json()["dictionary"] == []

    # A second job accepting the same pair crosses the threshold.
    job2 = _new_job(client, tmp_path)
    r = client.put(f"/jobs/{job2}/corrections", json={"corrections": [pair]})
    assert r.status_code == 200
    d = client.get("/dictionary").json()["dictionary"]
    assert d == [{"before": pair["before"], "after": pair["after"],
                  "count": 2}]

    # Reviewer skips it twice: gone from suggestions.
    for _ in range(2):
        r = client.post("/dictionary/skip",
                        json={"before": pair["before"], "after": pair["after"]})
        assert r.status_code == 200 and r.json()["skipped"] is True
    assert client.get("/dictionary").json()["dictionary"] == []

    # One more acceptance (third job) brings it back.
    job3 = _new_job(client, tmp_path)
    client.put(f"/jobs/{job3}/corrections", json={"corrections": [pair]})
    d = client.get("/dictionary").json()["dictionary"]
    assert d == [{"before": pair["before"], "after": pair["after"],
                  "count": 3}]

    # The corrections themselves are untouched by all of this.
    r = client.get(f"/jobs/{job1}/corrections")
    assert r.json()["corrections"] == [pair]


def test_dictionary_skip_validation(client):
    r = client.post("/dictionary/skip", json={"before": "", "after": "x"})
    assert r.status_code == 422


# --- Turso failure must never 500 the editor ------------------------------

class _BadTurso:
    """Stand-in libsql module whose connect() is rejected (e.g. 401)."""
    @staticmethod
    def connect(*a, **kw):
        raise RuntimeError("sync failed: 401 Unauthorized")


class _BadQueryConn:
    def execute(self, sql, *a):
        if sql.strip().upper().startswith("SELECT 1"):
            class _R:
                def fetchall(self):
                    return [(1,)]
            return _R()
        raise RuntimeError("stream error: 401")

    def close(self):
        pass


class _BadQueryTurso:
    @staticmethod
    def connect(*a, **kw):
        return _BadQueryConn()


def _turso_env(monkeypatch, module):
    monkeypatch.setenv("LIBSQL_URL", "libsql://example.turso.io")
    monkeypatch.setenv("LIBSQL_AUTH_TOKEN", "bad-token")
    monkeypatch.setattr(flywheel, "_libsql", module)
    monkeypatch.setattr(flywheel, "_turso_down_until", 0.0)


def test_bad_turso_token_falls_back_to_local(client, monkeypatch):
    _turso_env(monkeypatch, _BadTurso)
    assert flywheel.using_libsql() is True
    r = client.get("/dictionary")
    assert r.status_code == 200
    assert "dictionary" in r.json()
    assert flywheel.turso_active() is False  # backed off after the failure
    r = client.post("/dictionary/skip",
                    json={"before": "x", "after": "y"})
    assert r.status_code == 200
    assert flywheel._local_path().exists()


def test_turso_query_failure_falls_back_to_local(client, monkeypatch):
    _turso_env(monkeypatch, _BadQueryTurso)
    r = client.get("/dictionary")
    assert r.status_code == 200
    assert flywheel.turso_active() is False
