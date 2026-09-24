"""Each page records which OCR engine read it (page.ocr_engine) and whether
the route's first engine failed (page.ocr_fallback), so the editor can show
it. FAST -> Gemini, HEAVY -> Sarvam, the other engine is the fallback,
stub when neither produced text. No network.

Run from fastapi/sqlite/:
    python -m pytest tests/test_ocr_engine_label.py -v
"""

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import db, main, ocr, storage
from app.schema_out import build_page_result

LINE = [{"body": "கற்றதனால்", "bbox": [10, 10, 300, 30], "confidence": 0.9}]
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


def _boom(img, client=None):
    raise RuntimeError("engine down")


def _ok(img, client=None):
    return [dict(l) for l in LINE]


def _page(client, profile, monkeypatch):
    monkeypatch.setattr(main, "choose_profile", lambda s: (profile, s))
    r = client.post("/jobs", files={"file": ("p.png", _png(), "image/png")})
    assert r.status_code == 200, r.text
    job = client.get(f"/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "done"
    return job["result"]["pages"][0]


def test_fast_page_says_gemini(client, monkeypatch):
    monkeypatch.setattr(ocr, "_gemini_ocr", _ok)
    monkeypatch.setattr(ocr, "_sarvam_ocr", _boom)
    page = _page(client, "FAST", monkeypatch)
    assert page["ocr_engine"] == "gemini"
    assert page["ocr_fallback"] is False


def test_heavy_page_says_sarvam(client, monkeypatch):
    monkeypatch.setattr(ocr, "_gemini_ocr", _boom)
    monkeypatch.setattr(ocr, "_sarvam_ocr", _ok)
    page = _page(client, "HEAVY", monkeypatch)
    assert page["ocr_engine"] == "sarvam"
    assert page["ocr_fallback"] is False


def test_heavy_page_falls_back_to_gemini_and_says_so(client, monkeypatch):
    monkeypatch.setattr(ocr, "_gemini_ocr", _ok)
    monkeypatch.setattr(ocr, "_sarvam_ocr", _boom)
    page = _page(client, "HEAVY", monkeypatch)
    assert page["ocr_engine"] == "gemini"
    assert page["ocr_fallback"] is True


def test_both_engines_down_says_stub(client, monkeypatch):
    monkeypatch.setattr(ocr, "_gemini_ocr", _boom)
    monkeypatch.setattr(ocr, "_sarvam_ocr", _boom)
    page = _page(client, "FAST", monkeypatch)
    assert page["ocr_engine"] == "stub"
    assert page["ocr_fallback"] is True


def test_ocr_page_raising_marks_stub(client, monkeypatch):
    def explode(img, profile=None):
        raise RuntimeError("escaped")
    monkeypatch.setattr(main, "ocr_page", explode)
    page = _page(client, "FAST", monkeypatch)
    assert page["ocr_engine"] == "stub"


def test_unknown_engine_leaves_page_shape_unchanged(client, monkeypatch):
    # A stand-in ocr_page that never touches the engine record (as older
    # tests do): no ocr_engine key, exactly like pages before this change.
    monkeypatch.setattr(main, "ocr_page", lambda img, profile=None: ([dict(l) for l in LINE], 1.0))
    page = _page(client, "FAST", monkeypatch)
    assert "ocr_engine" not in page and "ocr_fallback" not in page


def test_build_page_result_omits_engine_when_none():
    page = build_page_result(1, "FAST", QUALITY, [dict(LINE[0], layout_confidence=0.9)], 5.0)
    assert "ocr_engine" not in page
    page = build_page_result(1, "FAST", QUALITY, [dict(LINE[0], layout_confidence=0.9)], 5.0,
                             ocr_engine="gemini", ocr_fallback=False)
    assert page["ocr_engine"] == "gemini" and page["ocr_fallback"] is False
