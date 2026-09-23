"""Corrections endpoint: GET/PUT /jobs/{job_id}/corrections.

Run from fastapi/sqlite/:
    python -m pytest tests/ -v
"""

import hashlib

import pytest
from fastapi.testclient import TestClient

from app import db, storage
from app.main import app

import cv2
import numpy as np

UNKNOWN = "0" * 32
FIX = {"page": 1, "line": "L3", "word": 5,
       "before": "வாழறிவன்", "after": "வாலறிவன்"}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("corrections")
    db.DB_PATH = tmp / "test.db"
    storage.UPLOAD_ROOT = tmp / "uploads"
    db.init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def job(client, tmp_path_factory):
    img = np.full((400, 600, 3), 235, np.uint8)
    cv2.putText(img, "TAMIL", (50, 200), cv2.FONT_HERSHEY_SIMPLEX, 2, (20, 20, 20), 3)
    ok, buf = cv2.imencode(".png", img)
    data = buf.tobytes()
    r = client.post("/jobs", files={"file": ("page.png", data, "image/png")})
    assert r.status_code == 200
    return r.json()["job_id"], hashlib.sha256(data).hexdigest()


def test_get_empty_before_any_save(client, job):
    job_id, _ = job
    r = client.get(f"/jobs/{job_id}/corrections")
    assert r.status_code == 200
    assert r.json() == {"job_id": job_id, "corrections": [], "updated_at": None}


def test_put_then_get_roundtrip(client, job):
    job_id, _ = job
    r = client.put(f"/jobs/{job_id}/corrections", json={"corrections": [FIX]})
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == job_id
    assert body["corrections"] == [FIX]
    assert body["updated_at"].endswith("+00:00")

    r = client.get(f"/jobs/{job_id}/corrections")
    assert r.status_code == 200
    assert r.json() == body
    assert (storage.UPLOAD_ROOT / job_id / "corrections.json").is_file()


def test_put_replaces_not_merges(client, job):
    job_id, _ = job
    other = dict(FIX, word=2, before="a", after="b")
    client.put(f"/jobs/{job_id}/corrections", json={"corrections": [FIX]})
    r = client.put(f"/jobs/{job_id}/corrections", json={"corrections": [other]})
    assert r.json()["corrections"] == [other]
    assert client.get(f"/jobs/{job_id}/corrections").json()["corrections"] == [other]


def test_empty_put_clears(client, job):
    job_id, _ = job
    client.put(f"/jobs/{job_id}/corrections", json={"corrections": [FIX]})
    r = client.put(f"/jobs/{job_id}/corrections", json={"corrections": []})
    assert r.status_code == 200
    assert r.json()["corrections"] == []
    assert client.get(f"/jobs/{job_id}/corrections").json()["corrections"] == []


@pytest.mark.parametrize("job_id", [UNKNOWN, "not-a-job"])
def test_unknown_job_404(client, job_id):
    for r in (client.get(f"/jobs/{job_id}/corrections"),
              client.put(f"/jobs/{job_id}/corrections", json={"corrections": []})):
        assert r.status_code == 404
        assert r.json() == {"detail": "job not found"}


@pytest.mark.parametrize("payload", [
    {},
    {"corrections": "nope"},
    {"corrections": [{"page": 1, "line": "L1"}]},
    {"corrections": [dict(FIX, page="one")]},
])
def test_bad_payload_422(client, job, payload):
    job_id, _ = job
    r = client.put(f"/jobs/{job_id}/corrections", json=payload)
    assert r.status_code == 422


def test_soft_cap_422(client, job):
    job_id, _ = job
    r = client.put(f"/jobs/{job_id}/corrections",
                   json={"corrections": [FIX] * 10_001})
    assert r.status_code == 422


def test_master_unaffected(client, job):
    job_id, sha = job
    client.put(f"/jobs/{job_id}/corrections", json={"corrections": [FIX]})
    assert storage.verify_master(job_id, sha)
    assert storage.master_path(job_id).name == "master.png"
    assert client.get(f"/jobs/{job_id}").json()["sha256"] == sha
