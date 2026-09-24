"""Upload dedup: re-uploading bytes a finished job already OCR'd returns
that job instantly - no new job row, no second (paid) OCR run. Failed or
in-flight matches never dedup: they start a fresh job.

Run from fastapi/sqlite/:
    python -m pytest tests/test_upload_dedup.py -v
"""

import json
import sqlite3
import uuid

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import db, main, storage


@pytest.fixture()
def client(tmp_path):
    db.DB_PATH = tmp_path / "test.db"
    storage.UPLOAD_ROOT = tmp_path / "uploads"
    db.init_db()
    with TestClient(main.app, raise_server_exceptions=True) as c:
        yield c


def _png(label="LINE") -> bytes:
    img = np.full((600, 900, 3), 235, np.uint8)
    cv2.putText(img, label, (80, 200), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (20, 20, 20), 3)
    return cv2.imencode(".png", img)[1].tobytes()


def _counting_ocr(calls: list):
    def ocr(img, profile=None):
        calls.append(1)
        return [{"body": "நல்ல வரி", "bbox": [10, 10, 500, 30], "confidence": 0.4}], 1.0
    return ocr


def _post_file(client, data, name="page.png"):
    r = client.post("/jobs", files={"file": (name, data, "image/png")})
    assert r.status_code == 200, r.text
    return r.json()


def _post_multi(client, parts):
    r = client.post(
        "/jobs",
        files=[("files", (n, d, "image/png")) for n, d in parts],
    )
    assert r.status_code == 200, r.text
    return r.json()


def _job_count():
    with sqlite3.connect(db.DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


def _seed_job(data: bytes, status: str, result_json: str | None) -> str:
    job_id = uuid.uuid4().hex
    db.create_job(job_id, "page.png", storage.sha256_bytes(data))
    if status != "pending":
        db.set_result(job_id, status, result_json)
    return job_id


def test_sha256_index_exists(client):
    with sqlite3.connect(db.DB_PATH) as conn:
        names = {row[1] for row in conn.execute("PRAGMA index_list('jobs')")}
    assert "idx_jobs_sha256" in names


def test_same_file_reupload_is_instant_and_free(client, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "ocr_page", _counting_ocr(calls))
    data = _png()

    first = _post_file(client, data)
    assert first["status"] == "pending"
    assert client.get(f"/jobs/{first['job_id']}").json()["status"] == "done"
    assert len(calls) == 1  # one page, one OCR call

    again = _post_file(client, data)
    assert again["job_id"] == first["job_id"]      # the EXISTING job
    assert again["status"] == "done"               # instantly finished
    assert again["duplicate"] is True
    assert len(calls) == 1                         # no second OCR run
    assert _job_count() == 1                       # no new job row

    # the dedup reply points at a fully usable job (editor loads this)
    job = client.get(f"/jobs/{again['job_id']}").json()
    assert job["status"] == "done" and job["result"]["pages"]


def test_edited_file_runs_fresh_ocr(client, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "ocr_page", _counting_ocr(calls))

    first = _post_file(client, _png("ONE"))
    edited = _post_file(client, _png("TWO"))  # different bytes, different hash

    assert edited["job_id"] != first["job_id"]
    assert edited["status"] == "pending"
    assert edited.get("duplicate") is not True
    assert len(calls) == 2
    assert _job_count() == 2


def test_failed_job_retries_ocr(client, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "ocr_page", _counting_ocr(calls))
    data = _png()
    failed_id = _seed_job(data, "error", json.dumps({"error": "boom"}))

    retry = _post_file(client, data)
    assert retry["job_id"] != failed_id            # fresh job, not the error row
    assert retry.get("duplicate") is not True
    assert client.get(f"/jobs/{retry['job_id']}").json()["status"] == "done"
    assert len(calls) == 1
    assert _job_count() == 2


def test_pending_job_does_not_dedup(client, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "ocr_page", _counting_ocr(calls))
    data = _png()
    pending_id = _seed_job(data, "pending", None)

    fresh = _post_file(client, data)
    assert fresh["job_id"] != pending_id
    assert fresh.get("duplicate") is not True
    assert _job_count() == 2


def test_done_job_with_corrupt_result_does_not_dedup(client, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "ocr_page", _counting_ocr(calls))
    data = _png()
    corrupt_id = _seed_job(data, "done", "{not valid json")

    fresh = _post_file(client, data)
    assert fresh["job_id"] != corrupt_id           # invalid result never served
    assert fresh.get("duplicate") is not True
    assert len(calls) == 1
    assert _job_count() == 2


def test_multi_image_same_set_dedups(client, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "ocr_page", _counting_ocr(calls))
    parts = [("a.png", _png("A")), ("b.png", _png("B"))]

    first = _post_multi(client, parts)
    assert client.get(f"/jobs/{first['job_id']}").json()["status"] == "done"
    assert len(calls) == 2  # one OCR call per image

    again = _post_multi(client, parts)
    assert again["job_id"] == first["job_id"]
    assert again["status"] == "done"
    assert again["duplicate"] is True
    assert len(calls) == 2
    assert _job_count() == 1


def test_multi_image_reordered_set_is_a_fresh_job(client, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "ocr_page", _counting_ocr(calls))
    a, b = ("a.png", _png("A")), ("b.png", _png("B"))

    first = _post_multi(client, [a, b])
    reordered = _post_multi(client, [b, a])  # concatenation hash is ordered

    assert reordered["job_id"] != first["job_id"]
    assert reordered.get("duplicate") is not True
    assert len(calls) == 4
    assert _job_count() == 2
