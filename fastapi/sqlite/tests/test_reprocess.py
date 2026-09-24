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


# --- crop: ?crop=x0,y0,x1,y1 fractions of the ROTATED page -----------------
# The env master is 80 x 200 (h x w), grey 220, with an "x" drawn around
# (20..40, 25..50). Crop tests paint a marker so they can prove which region
# survived, not just the output size.

def _render(client, job_id):
    assert client.get(f"/jobs/{job_id}/pages/1/image").status_code == 200
    return storage.UPLOAD_ROOT / job_id / "page-1.png"


def _paint_marker(path):
    """Black block in the top-right corner of the current render."""
    img = cv2.imread(str(path))
    img[0:20, 180:200] = 0
    assert cv2.imwrite(str(path), img)


def test_reprocess_crop_only(env):
    client, job_id = env
    path = _render(client, job_id)
    _paint_marker(path)
    # Right half of the page, full height.
    r = client.post(f"/jobs/{job_id}/pages/1/reprocess",
                    params={"rotate": 0, "crop": "0.5,0,1,1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["crop"] == [100, 0, 200, 80]
    assert (body["width"], body["height"]) == (100, 80)
    after = cv2.imread(str(path))
    assert after.shape[:2] == (80, 100)
    assert after[5, 95].max() == 0        # marker kept, now at x 80..100
    assert after[60, 5].min() == 220      # plain paper elsewhere
    pages = _pages(client, job_id)
    assert [ln["body"] for ln in pages[0]["lines"]] == [NEW_LINE]
    assert [ln["body"] for ln in pages[1]["lines"]] == ["old line two"]


def test_reprocess_crop_ocr_sees_the_cropped_image(env, monkeypatch):
    client, job_id = env
    seen = []

    def spy(img, profile=None):
        seen.append(img.shape[:2])
        return _fake_ocr(img, profile)

    monkeypatch.setattr(main, "ocr_page", spy)
    monkeypatch.setattr(main, "prepare",
                        lambda img: (img, main.Mapper((0, 0), 0.0, 1.0,
                                                      img.shape[:2])))
    r = client.post(f"/jobs/{job_id}/pages/1/reprocess",
                    params={"crop": "0.1,0.25,0.6,0.75"})
    assert r.status_code == 200, r.text
    assert seen == [(40, 100)]            # y 20..60, x 20..120


def test_reprocess_rotate_then_crop(env):
    """Rotate first, then crop in rotated coordinates: 90 deg clockwise turns
    the 200x80 page into 80x200 with the old top-right corner at the new
    bottom-right; cropping the bottom quarter must keep that marker."""
    client, job_id = env
    path = _render(client, job_id)
    _paint_marker(path)
    r = client.post(f"/jobs/{job_id}/pages/1/reprocess",
                    params={"rotate": 90, "crop": "0,0.75,1,1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["crop"] == [0, 150, 80, 200]
    after = cv2.imread(str(path))
    assert after.shape[:2] == (50, 80)    # 80 wide rotated, bottom 50 rows
    assert after[45, 75].max() == 0       # marker: bottom-right corner
    assert after[5, 5].min() == 220


def test_reprocess_edits_compose_on_current_render(env):
    """A second reprocess starts from the current (already cropped) render,
    so crop-then-rotate and repeated crops compose; original=true resets to
    the master."""
    client, job_id = env
    path = _render(client, job_id)
    r = client.post(f"/jobs/{job_id}/pages/1/reprocess",
                    params={"crop": "0,0,0.5,1"})
    assert r.status_code == 200, r.text
    assert cv2.imread(str(path)).shape[:2] == (80, 100)
    r = client.post(f"/jobs/{job_id}/pages/1/reprocess", params={"rotate": 90})
    assert r.status_code == 200, r.text
    assert cv2.imread(str(path)).shape[:2] == (100, 80)
    r = client.post(f"/jobs/{job_id}/pages/1/reprocess",
                    params={"crop": "0,0,1,0.5"})
    assert r.status_code == 200, r.text
    assert cv2.imread(str(path)).shape[:2] == (50, 80)
    r = client.post(f"/jobs/{job_id}/pages/1/reprocess",
                    params={"original": "true"})
    assert r.status_code == 200, r.text
    assert r.json()["original"] is True
    assert cv2.imread(str(path)).shape[:2] == (80, 200)


def test_reprocess_crop_drops_that_pages_corrections(env):
    client, job_id = env
    r = client.put(f"/jobs/{job_id}/corrections", json={"corrections": [
        {"page": 1, "line": "L1", "word": 1, "before": "old", "after": "new"},
        {"page": 2, "line": "L1", "word": 1, "before": "old", "after": "new"},
    ]})
    assert r.status_code == 200, r.text
    r = client.post(f"/jobs/{job_id}/pages/1/reprocess",
                    params={"crop": "0.2,0.2,0.8,0.8"})
    assert r.status_code == 200, r.text
    body = client.get(f"/jobs/{job_id}/corrections").json()
    assert [c["page"] for c in body["corrections"]] == [2]


@pytest.mark.parametrize("crop", [
    "0,0,1",              # too few values
    "0,0,1,1,1",          # too many
    "a,0,1,1",            # not a number
    "nan,0,1,1",          # not finite
    "-0.1,0,1,1",         # below 0
    "0,0,1.5,1",          # above 1
    "0.5,0,0.5,1",        # empty width
    "0,0.8,1,0.2",        # inverted
    "0,0,0.05,1",         # 10 px wide: under the 16 px minimum
])
def test_reprocess_crop_guards(env, crop):
    client, job_id = env
    path = _render(client, job_id)
    before = _pages(client, job_id)
    r = client.post(f"/jobs/{job_id}/pages/1/reprocess",
                    params={"rotate": 0, "crop": crop})
    assert r.status_code == 422, (crop, r.text)
    # Nothing changed: lines and render untouched.
    assert _pages(client, job_id) == before
    assert cv2.imread(str(path)).shape[:2] == (80, 200)


def test_reprocess_blank_crop_means_no_crop(env):
    client, job_id = env
    r = client.post(f"/jobs/{job_id}/pages/1/reprocess",
                    params={"rotate": 0, "crop": ""})
    assert r.status_code == 200, r.text
    assert "crop" not in r.json()
