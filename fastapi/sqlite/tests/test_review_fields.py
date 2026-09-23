"""Tests for per-line review fields (slice 2): GET /jobs/{id} lines carry
confidence + needs_review, suggestions[] is an optional passthrough, and
schema/doc_demo.json validates against schema/schema.json.

Run from fastapi/sqlite/:
    python -m pytest tests/test_review_fields.py -v
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, storage
from app.main import app
from app.schema_out import build_job_result, build_page_result

QUALITY = {"blur": 1.0, "contrast": 1.0, "noise": 1.0, "skew_deg": 0.0}


def _lines(*specs):
    return [
        {"body": body, "bbox": [10, 10 + i * 40, 500, 30], "confidence": conf}
        for i, (body, conf) in enumerate(specs)
    ]


# ---- build_page_result: per-line needs_review ------------------------------

def test_per_line_needs_review_follows_the_receipt_rule():
    page = build_page_result(
        page_number=1, profile="FAST", quality=QUALITY,
        ocr_lines=_lines(("நல்ல வரி", 0.9), ("மங்கலான வரி", 0.4),
                         ("[stub] OCR unavailable", 0.0)),
        processing_ms=1.0)
    assert page["lines"][0]["needs_review"] is False   # clean, above floor
    assert page["lines"][1]["needs_review"] is True    # below the 0.5 floor
    assert page["lines"][2]["needs_review"] is True    # stub output


def test_heavy_page_flags_every_line():
    page = build_page_result(
        page_number=1, profile="HEAVY", quality=QUALITY,
        ocr_lines=_lines(("நல்ல வரி", 0.99)), processing_ms=1.0)
    assert page["needs_review"] is True
    assert page["lines"][0]["needs_review"] is True


def test_suggestions_passthrough_optional():
    sugg = [{"line": "L1", "word": 4, "before": "வாழறிவன்",
             "candidates": [{"text": "வாலறிவன்", "score": 0.93,
                             "source": "lexicon"}]}]
    with_s = build_page_result(1, "HEAVY", QUALITY,
                               _lines(("கற்றதனால் ஆய பயனென்கொல் வாழறிவன்", 0.4)),
                               1.0, suggestions=sugg)
    assert with_s["suggestions"] == sugg
    without = build_page_result(1, "FAST", QUALITY, _lines(("நல்ல வரி", 0.9)), 1.0)
    assert "suggestions" not in without  # absent, not null/empty


# ---- GET /jobs/{id}: legacy stored results get back-filled -----------------

@pytest.fixture()
def client(tmp_path):
    db.DB_PATH = tmp_path / "test.db"
    storage.UPLOAD_ROOT = tmp_path / "uploads"
    db.init_db()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _legacy_result():
    """A result JSON as jobs stored BEFORE per-line needs_review existed."""
    page = build_page_result(1, "FAST", QUALITY,
                             _lines(("நல்ல வரி", 0.9), ("மங்கலான வரி", 0.2)), 1.0)
    for line in page["lines"]:
        del line["needs_review"]
    page["suggestions"] = [{"line": "L2", "word": 1, "before": "மங்கலான",
                            "candidates": [{"text": "மங்கலம்"}]}]
    return json.dumps(build_job_result([page]), ensure_ascii=False)


def test_get_job_backfills_line_flags_and_passes_suggestions(client):
    job_id = "d" * 32
    db.create_job(job_id, "legacy.pdf", "6" * 64)
    db.set_result(job_id, "done", _legacy_result())

    r = client.get(f"/jobs/{job_id}")
    assert r.status_code == 200, r.text
    page = r.json()["result"]["pages"][0]
    flags = {l["id"]: l["needs_review"] for l in page["lines"]}
    assert flags == {"L1": False, "L2": True}
    for line in page["lines"]:
        assert "confidence" in line
    # stored suggestions[] survives the trip untouched
    assert page["suggestions"][0]["candidates"][0]["text"] == "மங்கலம்"


def test_get_job_pending_result_is_null(client):
    job_id = "2" * 32
    db.create_job(job_id, "pending.pdf", "5" * 64)
    r = client.get(f"/jobs/{job_id}")
    assert r.status_code == 200
    assert r.json()["result"] is None


# ---- schema stays in sync with the demo document ----------------------------

def test_doc_demo_validates_against_schema():
    jsonschema = pytest.importorskip("jsonschema")
    root = Path(__file__).resolve().parents[3]
    schema = json.loads((root / "schema" / "schema.json").read_text(encoding="utf-8"))
    demo = json.loads((root / "schema" / "doc_demo.json").read_text(encoding="utf-8"))
    jsonschema.validate(demo, schema)


def test_emitted_page_validates_against_schema():
    """What build_page_result emits must pass the closed contract schema."""
    jsonschema = pytest.importorskip("jsonschema")
    root = Path(__file__).resolve().parents[3]
    schema = json.loads((root / "schema" / "schema.json").read_text(encoding="utf-8"))
    page = build_page_result(
        1, "HEAVY", QUALITY,
        _lines(("கற்றதனால் ஆய பயனென்கொல் வாழறிவன்", 0.4)), 12.0,
        suggestions=[{"line": "L1", "word": 4, "before": "வாழறிவன்",
                      "candidates": [{"text": "வாலறிவன்", "score": 0.93,
                                      "source": "lexicon"}]}])
    jsonschema.validate(page, schema)
