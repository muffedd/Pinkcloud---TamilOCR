"""Corrections endpoint hardening: concurrent PUT/GET on one job, and the
field bounds on Correction.

Run from fastapi/sqlite/:
    python -m pytest tests/test_corrections_hardening.py -v
"""

import threading

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import db, storage
from app.main import app

FIX = {"page": 1, "line": "L3", "word": 5,
       "before": "வாழறிவன்", "after": "வாலறிவன்"}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("corr_hardening")
    db.DB_PATH = tmp / "test.db"
    storage.UPLOAD_ROOT = tmp / "uploads"
    db.init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def job_id(client):
    ok, buf = cv2.imencode(".png", np.full((300, 600, 3), 240, np.uint8))
    r = client.post("/jobs", files={"file": ("p.png", buf.tobytes(), "image/png")})
    assert r.status_code == 200
    return r.json()["job_id"]


def test_concurrent_puts_and_gets_never_500(client, job_id):
    big = [dict(FIX, page=1 + i // 100, line=f"L{i % 100 + 1}", word=1 + i % 7)
           for i in range(1500)]
    codes, lock = [], threading.Lock()

    def writer(n):
        for k in range(6):
            r = client.put(f"/jobs/{job_id}/corrections",
                           json={"corrections": big[: 1000 + n * 50 + k]})
            with lock:
                codes.append(("PUT", r.status_code))

    def reader():
        for _ in range(20):
            r = client.get(f"/jobs/{job_id}/corrections")
            with lock:
                codes.append(("GET", r.status_code))

    ts = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    ts += [threading.Thread(target=reader) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    bad = [c for c in codes if c[1] != 200]
    assert not bad, bad
    assert len(codes) == 4 * 6 + 2 * 20
    # Final file is a complete document from one of the writers.
    r = client.get(f"/jobs/{job_id}/corrections")
    assert r.status_code == 200
    assert len(r.json()["corrections"]) in {1000 + n * 50 + k
                                            for n in range(4) for k in range(6)}
    # No temp files left behind.
    job_dir = storage.UPLOAD_ROOT / job_id
    assert not list(job_dir.glob("*.tmp"))


@pytest.mark.parametrize("patch", [
    {"page": 0},
    {"word": 0},
    {"line": ""},
    {"line": "L" * 33},
    {"after": ""},
    {"after": "   "},
    {"after": "a" * 257},
    {"before": "b" * 257},
])
def test_field_bounds_422(client, job_id, patch):
    r = client.put(f"/jobs/{job_id}/corrections",
                   json={"corrections": [dict(FIX, **patch)]})
    assert r.status_code == 422


def test_after_is_stripped_and_edges_accepted(client, job_id):
    edge = dict(FIX, line="L" * 32, before="b" * 256, after="  x  ")
    r = client.put(f"/jobs/{job_id}/corrections", json={"corrections": [edge]})
    assert r.status_code == 200
    assert r.json()["corrections"][0]["after"] == "x"
    r = client.put(f"/jobs/{job_id}/corrections",
                   json={"corrections": [dict(FIX, before="")]})
    assert r.status_code == 200  # empty "before" stays allowed
