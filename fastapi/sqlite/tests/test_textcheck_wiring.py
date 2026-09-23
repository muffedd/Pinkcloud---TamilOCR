"""Text check wired into the pipeline: line confidence / needs_review come
from textcheck.score_line (a "text looks malformed" proxy), not Sarvam's
layout-block score; HEAVY pages flag only their bad lines; old stored jobs
are re-scored on read without rewriting the DB.

Run from fastapi/sqlite/:
    python -m pytest tests/test_textcheck_wiring.py -v
"""

import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import db, main, storage
from app.schema_out import build_job_result, build_page_result
from app.textcheck import score_line

CLEAN = "கற்றதனால் ஆய பயனென்கொல் வாலறிவன்"
BAD = "\u0bcd\u0bcdவரி\ufffd\ufffd\ufffd"          # orphan viramas + U+FFFD
QUALITY = {"blur": 1.0, "contrast": 1.0, "noise": 1.0, "skew_deg": 0.0}


@pytest.fixture()
def client(tmp_path):
    db.DB_PATH = tmp_path / "test.db"
    storage.UPLOAD_ROOT = tmp_path / "uploads"
    db.init_db()
    with TestClient(main.app, raise_server_exceptions=True) as c:
        yield c


def _png() -> bytes:
    img = np.full((600, 900, 3), 235, np.uint8)
    cv2.putText(img, "LINE", (80, 200), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (20, 20, 20), 3)
    return cv2.imencode(".png", img)[1].tobytes()


def _fake_ocr(lines):
    """ocr_page stand-in: (body, layout_score) pairs -> engine lines."""
    def ocr(img):
        return [{"body": b, "bbox": [10, 10 + i * 40, 500, 30], "confidence": c}
                for i, (b, c) in enumerate(lines)], 1.0
    return ocr


def _post_and_get(client):
    r = client.post("/jobs", files={"file": ("p.png", _png(), "image/png")})
    assert r.status_code == 200, r.text
    job = client.get(f"/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "done"
    return job


def test_line_confidence_comes_from_textcheck_not_layout_score(client, monkeypatch):
    # Layout scores are deliberately the OPPOSITE of the text quality.
    monkeypatch.setattr(main, "ocr_page", _fake_ocr([(CLEAN, 0.25), (BAD, 0.97)]))
    monkeypatch.setattr(main, "choose_profile", lambda s: ("FAST", s))
    lines = _post_and_get(client)["result"]["pages"][0]["lines"]
    clean, bad = lines
    assert clean["confidence"] == score_line(CLEAN) >= 0.95
    assert clean["layout_confidence"] == 0.25
    assert clean["needs_review"] is False
    assert bad["confidence"] == score_line(BAD) < 0.80
    assert bad["layout_confidence"] == 0.97
    assert bad["needs_review"] is True


def test_stub_lines_stay_at_zero_and_flagged(client, monkeypatch):
    monkeypatch.setattr(main, "ocr_page", _fake_ocr([("[stub] OCR unavailable", 0.0)]))
    page = _post_and_get(client)["result"]["pages"][0]
    assert page["lines"][0]["confidence"] == 0.0
    assert page["lines"][0]["needs_review"] is True
    assert page["needs_review"] is True


def test_heavy_page_flags_only_bad_lines_and_receipt_agrees(client, monkeypatch):
    specs = [(CLEAN, 0.3)] * 33
    bad_at = {4, 11, 20, 29}
    specs = [(BAD, 0.3) if i in bad_at else s for i, s in enumerate(specs)]
    monkeypatch.setattr(main, "ocr_page", _fake_ocr(specs))
    monkeypatch.setattr(main, "choose_profile", lambda s: ("HEAVY", s))
    job = _post_and_get(client)
    page = job["result"]["pages"][0]
    assert page["profile"] == "HEAVY"
    assert page["needs_review"] is True           # page-level flag unchanged
    flagged = [l for l in page["lines"] if l["needs_review"]]
    assert len(flagged) == 4                       # not all 33
    assert all(l["body"] == BAD for l in flagged)

    rc = client.get(f"/jobs/{job['job_id']}/receipt").json()
    assert rc["lines"] == {"total": 33, "auto": 29, "human_review": 4}
    assert rc["per_page"][0]["lines_human_review"] == 4
    assert rc["pages"]["human_review"] == 1
    assert rc["ocr"]["review_floor"] == 0.8


def _old_job_result():
    """A HEAVY page as stored before the text check: confidence is Sarvam's
    layout score and every line was forced to needs_review by HEAVY."""
    page = build_page_result(1, "HEAVY", QUALITY, [
        {"body": CLEAN, "bbox": [10, 10, 500, 30], "confidence": 0.31},
        {"body": BAD, "bbox": [10, 50, 500, 30], "confidence": 0.88},
        {"body": CLEAN, "bbox": [10, 90, 500, 30], "confidence": 0.31},
    ], 1.0)
    for line in page["lines"]:
        line["needs_review"] = True
        assert "layout_confidence" not in line
    return json.dumps(build_job_result([page]), ensure_ascii=False)


def test_old_job_backfill_uses_textcheck_without_rewriting_db(client):
    job_id = "e" * 32
    stored = _old_job_result()
    db.create_job(job_id, "old.png", "7" * 64)
    db.set_result(job_id, "done", stored)

    page = client.get(f"/jobs/{job_id}").json()["result"]["pages"][0]
    assert [l["needs_review"] for l in page["lines"]] == [False, True, False]
    assert [l["layout_confidence"] for l in page["lines"]] == [0.31, 0.88, 0.31]
    assert page["lines"][0]["confidence"] == score_line(CLEAN)
    assert page["lines"][1]["confidence"] == score_line(BAD)
    assert page["needs_review"] is True            # page flag as stored

    # receipt counts the same lines the API flags
    storage.save_master(job_id, "old.png", _png())
    rc = client.get(f"/jobs/{job_id}/receipt").json()
    assert rc["lines"]["human_review"] == 1

    # stored result untouched
    assert db.get_job(job_id)["result_json"] == stored


def test_new_page_validates_against_schema(client, monkeypatch):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((Path(__file__).resolve().parents[3]
                         / "schema" / "schema.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(main, "ocr_page", _fake_ocr([(CLEAN, 0.4), (BAD, 0.9)]))
    page = _post_and_get(client)["result"]["pages"][0]
    jsonschema.validate(page, schema)
