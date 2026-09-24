"""POST /jobs answers with the job_id at once; OCR runs on a background
thread (one per job, pages in order) and GET /jobs/{id} goes
pending -> done | error.

Run from fastapi/sqlite/:
    python -m pytest tests/test_async_jobs.py -v
"""

import threading
import time

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import db, main, storage

pytestmark = pytest.mark.async_jobs


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


def _gated_ocr(gate: threading.Event, calls: list):
    def ocr(img, profile=None):
        calls.append(threading.current_thread().name)
        assert gate.wait(10), "test gate never opened"
        return [{"body": "நல்ல வரி", "bbox": [10, 10, 500, 30], "confidence": 0.4}], 1.0
    return ocr


def _wait_status(client, job_id, want, timeout=10.0):
    t0 = time.monotonic()
    seen = []
    while time.monotonic() - t0 < timeout:
        job = client.get(f"/jobs/{job_id}").json()
        if not seen or seen[-1] != job["status"]:
            seen.append(job["status"])
        if job["status"] == want:
            return job, seen
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached {want}; saw {seen}")


def test_post_returns_before_ocr_and_poll_goes_pending_to_done(client, monkeypatch):
    gate, calls = threading.Event(), []
    monkeypatch.setattr(main, "ocr_page", _gated_ocr(gate, calls))

    t0 = time.monotonic()
    r = client.post("/jobs", files={"file": ("p.png", _png(), "image/png")})
    elapsed = time.monotonic() - t0
    assert r.status_code == 200, r.text
    body = r.json()
    job_id = body["job_id"]
    assert len(job_id) == 32 and body["status"] == "pending"
    assert elapsed < 5  # OCR is blocked on the gate, so POST did not wait for it

    pending = client.get(f"/jobs/{job_id}").json()
    assert pending["status"] == "pending"
    assert pending["result"] is None
    assert pending["pages_done"] == 0

    # the job also shows as pending in the list, with null counts
    row = client.get("/jobs").json()["jobs"][0]
    assert row["job_id"] == job_id and row["status"] == "pending"
    assert row["page_count"] is None

    gate.set()
    done, seen = _wait_status(client, job_id, "done")
    assert seen[0] == "pending" and seen[-1] == "done"
    assert "progress" not in done and "pages_done" not in done
    assert done["result"]["pages"][0]["lines"][0]["body"] == "நல்ல வரி"
    assert calls and calls[0].startswith("pinkcloud-job-")
    assert main.job_progress(job_id) is None


def test_progress_counts_pages_of_a_multi_image_job(client, monkeypatch):
    gates = [threading.Event(), threading.Event()]

    def ocr(img, profile=None):
        # pages OCR concurrently: key on the page, not the call order
        i = main.current_page() - 1
        assert gates[i].wait(10)
        return [{"body": f"வரி {i}", "bbox": [10, 10, 500, 30], "confidence": 0.5}], 1.0

    monkeypatch.setattr(main, "ocr_page", ocr)
    files = [("files", ("a.png", _png("A"), "image/png")),
             ("files", ("b.png", _png("B"), "image/png"))]
    r = client.post("/jobs", files=files)
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    gates[0].set()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 10:
        job = client.get(f"/jobs/{job_id}").json()
        if job.get("pages_done") == 1:
            break
        time.sleep(0.02)
    assert job["status"] == "pending"
    assert (job["pages_done"], job["pages_total"], job["progress"]) == (1, 2, 50)

    gates[1].set()
    done, _ = _wait_status(client, job_id, "done")
    assert [p["page"] for p in done["result"]["pages"]] == [1, 2]
    assert [p["lines"][0]["body"] for p in done["result"]["pages"]] == ["வரி 0", "வரி 1"]


def test_background_failure_marks_job_error_with_scrubbed_message(client, monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "sk-test-secret-123")

    def boom(job_id, master):
        raise RuntimeError("upstream said no for key sk-test-secret-123")

    monkeypatch.setattr(main, "_pipeline", boom)
    r = client.post("/jobs", files={"file": ("p.png", _png(), "image/png")})
    assert r.status_code == 200
    job, _ = _wait_status(client, r.json()["job_id"], "error")
    err = job["result"]["error"]
    assert "sk-test-secret-123" not in err and "***" in err
    assert err.startswith("RuntimeError:")
    row = client.get("/jobs").json()["jobs"][0]
    assert row["status"] == "error" and "sk-test-secret-123" not in row["error"]


def test_multi_image_failure_marks_job_error(client, monkeypatch):
    def boom(job_id, master):
        raise RuntimeError("page loader broke")

    monkeypatch.setattr(main, "_pipeline", boom)
    files = [("files", ("a.png", _png("A"), "image/png")),
             ("files", ("b.png", _png("B"), "image/png"))]
    r = client.post("/jobs", files=files)
    assert r.status_code == 200
    job, _ = _wait_status(client, r.json()["job_id"], "error")
    assert "page loader broke" in job["result"]["error"]


def test_thread_start_failure_marks_job_error(client, monkeypatch):
    def no_threads(target, *args):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(main, "_spawn_job", no_threads)
    r = client.post("/jobs", files={"file": ("p.png", _png(), "image/png")})
    assert r.status_code == 200
    job = client.get(f"/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "error"
    assert "can't start new thread" in job["result"]["error"]


def test_validation_errors_still_answer_before_any_job(client):
    r = client.post("/jobs", files={"file": ("x.exe", b"MZ", "image/png")})
    assert r.status_code == 400
    assert client.get("/jobs").json()["total"] == 0
