"""POST /jobs/{id}/pages/{n}/reprocess?rotate=deg: re-run OCR for one page
of a finished job, with the stored page rotated clockwise first. Pins: the
page's lines are replaced (other pages untouched), the rendered page image
cache is rotated to match, the page's saved corrections are dropped (other
pages keep theirs), and the 404/409/422 guards.

Run from fastapi/sqlite/:
    python -m pytest tests/test_reprocess.py -v
"""

import json

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import db, main, storage
from app.main import app
from app.schema_out import build_job_result, build_page_result

NEW_LINE = "புதிய வரி ஒன்று"


def _page(n, bodies):
    lines = [{"body": b, "bbox": [10, 10 + i * 40, 500, 30], "confidence": 0.95}
             for i, b in enumerate(bodies)]
    return build_page_result(
        page_number=n, profile="FAST",
        quality={"blur": 1.0, "contrast": 1.0, "noise": 1.0, "skew_deg": 0.0},
        ocr_lines=lines, processing_ms=1.0)


def _fake_ocr(img, profile=None):
    h, w = img.shape[:2]
    return ([{"body": NEW_LINE, "bbox": [5, 5, w - 10, 40]}], 1.0)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db.DB_PATH = tmp_path / "test.db"
    storage.UPLOAD_ROOT = tmp_path / "uploads"
    db.init_db()
    job_id = "b" * 32
    db.create_job(job_id, "leaf.png", "8" * 64)
    db.set_result(job_id, "done", json.dumps(
        build_job_result([_page(1, ["old line one"]), _page(2, ["old line two"])]),
        ensure_ascii=False))
    # A wide (palm-leaf aspect) master the route can re-render and rotate.
    job_dir = storage.UPLOAD_ROOT / job_id
    job_dir.mkdir(parents=True)
    img = np.full((80, 200, 3), 220, dtype=np.uint8)
    cv2.putText(img, "x", (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)
    assert cv2.imwrite(str(job_dir / "master.png"), img)
    monkeypatch.setattr(main, "ocr_page", _fake_ocr)
    with TestClient(app, raise_server_exceptions=True) as client:
        yield client, job_id


def _pages(client, job_id):
    r = client.get(f"/jobs/{job_id}")
    assert r.status_code == 200, r.text
    return r.json()["result"]["pages"]


def test_reprocess_replaces_only_that_page(env):
    client, job_id = env
    r = client.post(f"/jobs/{job_id}/pages/1/reprocess", params={"rotate": 0})
    assert r.status_code == 200, r.text
    assert r.json() == {"job_id": job_id, "page": 1, "rotate": 0,
                        "status": "done"}
    pages = _pages(client, job_id)
    assert [ln["body"] for ln in pages[0]["lines"]] == [NEW_LINE]
    assert [ln["body"] for ln in pages[1]["lines"]] == ["old line two"]


def test_reprocess_rotate_turns_the_page_image(env):
    client, job_id = env
    before = cv2.imread(str(storage.UPLOAD_ROOT / job_id / "page-1.png"))
    # No cached render yet: force one via the image route.
    r = client.get(f"/jobs/{job_id}/pages/1/image")
    assert r.status_code == 200
    before = cv2.imread(str(storage.UPLOAD_ROOT / job_id / "page-1.png"))
    assert before.shape[:2] == (80, 200)

    r = client.post(f"/jobs/{job_id}/pages/1/reprocess", params={"rotate": 90})
    assert r.status_code == 200, r.text
    after = cv2.imread(str(storage.UPLOAD_ROOT / job_id / "page-1.png"))
    assert after.shape[:2] == (200, 80)  # rotated a quarter turn
    pages = _pages(client, job_id)
    assert [ln["body"] for ln in pages[0]["lines"]] == [NEW_LINE]


def test_reprocess_drops_only_that_pages_corrections(env):
    client, job_id = env
    r = client.put(f"/jobs/{job_id}/corrections", json={"corrections": [
        {"page": 1, "line": "L1", "word": 1, "before": "old", "after": "new"},
        {"page": 2, "line": "L1", "word": 1, "before": "old", "after": "new"},
    ]})
    assert r.status_code == 200, r.text
    r = client.post(f"/jobs/{job_id}/pages/1/reprocess", params={"rotate": 0})
    assert r.status_code == 200, r.text
    body = client.get(f"/jobs/{job_id}/corrections").json()
    assert [c["page"] for c in body["corrections"]] == [2]


def test_reprocess_guards(env):
    client, job_id = env
    assert client.post(
        f"/jobs/{job_id}/pages/1/reprocess", params={"rotate": 45}
    ).status_code == 422
    assert client.post(
        f"/jobs/{job_id}/pages/9/reprocess", params={"rotate": 0}
    ).status_code == 404
    assert client.post(
        f"/jobs/{'c' * 32}/pages/1/reprocess", params={"rotate": 0}
    ).status_code == 404

    pending = "d" * 32
    db.create_job(pending, "leaf.png", "7" * 64)
    assert client.post(
        f"/jobs/{pending}/pages/1/reprocess", params={"rotate": 0}
    ).status_code == 409
