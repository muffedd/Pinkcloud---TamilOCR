"""OCR mode selector (POST /jobs form field `mode`: auto | light | heavy).

  auto  (default) -> the router's per-page FAST/HEAVY decision, unchanged
  light           -> every page FAST: Gemini first, Sarvam fallback
  heavy           -> every page HEAVY: Sarvam first, Gemini fallback

Forced modes never call the router's decision (choose_profile). The mode is
stored on the job row (old databases get the column by ALTER) and echoed
by POST /jobs, GET /jobs/{id} and GET /jobs. No network.

Run from fastapi/sqlite/:
    python -m pytest tests/test_mode_selector.py -v
"""

import sqlite3

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import db, main, ocr, storage

LINE = [{"body": "கற்றதனால்", "bbox": [10, 10, 300, 30], "confidence": 0.9}]


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


def _pdf(pages: int) -> bytes:
    from PIL import Image
    import io
    imgs = [Image.fromarray(cv2.cvtColor(cv2.imdecode(
        np.frombuffer(_png(f"P{i}"), np.uint8), cv2.IMREAD_COLOR),
        cv2.COLOR_BGR2RGB)) for i in range(pages)]
    buf = io.BytesIO()
    imgs[0].save(buf, "PDF", save_all=True, append_images=imgs[1:])
    return buf.getvalue()


def _ok(img, client=None):
    return [dict(l) for l in LINE]


def _boom(img, client=None):
    raise RuntimeError("engine down")


class _Spy:
    """Counts calls to the router's decision; returns a fixed badge."""

    def __init__(self, badge="FAST"):
        self.calls = 0
        self.badge = badge

    def __call__(self, scores):
        self.calls += 1
        return self.badge, scores


def _engines(monkeypatch, gemini=_ok, sarvam=_ok):
    calls = {"gemini": 0, "sarvam": 0}

    def g(img, client=None):
        calls["gemini"] += 1
        return gemini(img)

    def s(img, client=None):
        calls["sarvam"] += 1
        return sarvam(img)

    monkeypatch.setattr(ocr, "_gemini_ocr", g)
    monkeypatch.setattr(ocr, "_sarvam_ocr", s)
    return calls


_SEQ = {"n": 0}


def _unique_png() -> bytes:
    """Fresh bytes per upload, so upload dedup never short-circuits a
    routing test."""
    _SEQ["n"] += 1
    return _png(f"L{_SEQ['n']}")


def _post(client, mode=None, data=None, **kw):
    form = {} if mode is None else {"mode": mode}
    files = kw.get("files") or {"file": ("p.png", data or _unique_png(), "image/png")}
    return client.post("/jobs", files=files, data=form)


def _done(client, r):
    assert r.status_code == 200, r.text
    job = client.get(f"/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "done", job
    return job


# ---- stored + echoed ------------------------------------------------------

@pytest.mark.parametrize("mode", ["auto", "light", "heavy"])
def test_mode_is_stored_and_echoed(client, monkeypatch, mode):
    _engines(monkeypatch)
    r = _post(client, mode)
    assert r.json()["mode"] == mode
    job = _done(client, r)
    assert job["mode"] == mode
    assert db.get_job(job["job_id"])["mode"] == mode
    row = next(j for j in client.get("/jobs").json()["jobs"]
               if j["job_id"] == job["job_id"])
    assert row["mode"] == mode


def test_mode_is_case_and_space_tolerant(client, monkeypatch):
    _engines(monkeypatch)
    assert _post(client, "  Heavy ").json()["mode"] == "heavy"


def test_missing_or_blank_mode_defaults_to_auto(client, monkeypatch):
    _engines(monkeypatch)
    assert _done(client, _post(client))["mode"] == "auto"
    assert _done(client, _post(client, ""))["mode"] == "auto"


@pytest.mark.parametrize("bad", ["turbo", "fast", "HEAVY!", "1"])
def test_invalid_mode_rejected_before_any_job(client, monkeypatch, bad):
    calls = _engines(monkeypatch)
    r = _post(client, bad)
    assert r.status_code == 422
    assert "mode" in r.json()["detail"]
    assert client.get("/jobs").json()["total"] == 0
    assert calls == {"gemini": 0, "sarvam": 0}


# ---- routing --------------------------------------------------------------

def test_light_forces_gemini_without_router(client, monkeypatch):
    spy = _Spy("HEAVY")  # router would say HEAVY; light must ignore it
    monkeypatch.setattr(main, "choose_profile", spy)
    calls = _engines(monkeypatch)
    job = _done(client, _post(client, "light",
                              files={"file": ("d.pdf", _pdf(3), "application/pdf")}))
    pages = job["result"]["pages"]
    assert len(pages) == 3
    assert spy.calls == 0
    assert all(p["profile"] == "FAST" for p in pages)
    assert all(p["ocr_engine"] == "gemini" and p["ocr_fallback"] is False for p in pages)
    assert calls == {"gemini": 3, "sarvam": 0}
    # quality metrics still measured (the contract requires them)
    assert set(pages[0]["quality"]) == {"blur", "contrast", "noise", "skew_deg"}


def test_heavy_forces_sarvam_without_router(client, monkeypatch):
    spy = _Spy("FAST")  # router would say FAST; heavy must ignore it
    monkeypatch.setattr(main, "choose_profile", spy)
    calls = _engines(monkeypatch)
    files = [("files", ("a.png", _png("A"), "image/png")),
             ("files", ("b.png", _png("B"), "image/png"))]
    job = _done(client, _post(client, "heavy", files=files))
    pages = job["result"]["pages"]
    assert spy.calls == 0
    assert [p["profile"] for p in pages] == ["HEAVY", "HEAVY"]
    assert all(p["ocr_engine"] == "sarvam" and p["ocr_fallback"] is False for p in pages)
    assert calls == {"gemini": 0, "sarvam": 2}


def test_auto_keeps_router_decision(client, monkeypatch):
    spy = _Spy("HEAVY")
    monkeypatch.setattr(main, "choose_profile", spy)
    calls = _engines(monkeypatch)
    job = _done(client, _post(client))  # no mode field at all
    page = job["result"]["pages"][0]
    assert spy.calls == 1
    assert page["profile"] == "HEAVY" and page["ocr_engine"] == "sarvam"
    assert calls == {"gemini": 0, "sarvam": 1}

    spy.badge = "FAST"
    job = _done(client, _post(client, "auto"))
    page = job["result"]["pages"][0]
    assert spy.calls == 2
    assert page["profile"] == "FAST" and page["ocr_engine"] == "gemini"


def test_forced_modes_keep_cross_fallback_labels(client, monkeypatch):
    # light: Gemini down -> Sarvam reads it, marked as fallback
    _engines(monkeypatch, gemini=_boom)
    page = _done(client, _post(client, "light"))["result"]["pages"][0]
    assert page["profile"] == "FAST"
    assert page["ocr_engine"] == "sarvam" and page["ocr_fallback"] is True
    # heavy: Sarvam down -> Gemini reads it, marked as fallback
    _engines(monkeypatch, sarvam=_boom)
    page = _done(client, _post(client, "heavy"))["result"]["pages"][0]
    assert page["profile"] == "HEAVY"
    assert page["ocr_engine"] == "gemini" and page["ocr_fallback"] is True
    # both down -> stub, still labelled against the forced route
    _engines(monkeypatch, gemini=_boom, sarvam=_boom)
    page = _done(client, _post(client, "heavy"))["result"]["pages"][0]
    assert page["ocr_engine"] == "stub" and page["ocr_fallback"] is True


def test_page_profile_unit(monkeypatch):
    spy = _Spy("HEAVY")
    monkeypatch.setattr(main, "choose_profile", spy)
    gray = np.full((200, 300), 235, np.uint8)
    assert main.page_profile(gray, "light")[0] == "FAST"
    assert main.page_profile(gray, "heavy")[0] == "HEAVY"
    assert spy.calls == 0
    assert main.page_profile(gray, "auto")[0] == "HEAVY"
    assert main.page_profile(gray)[0] == "HEAVY"
    assert spy.calls == 2


# ---- upload dedup is per mode ----------------------------------------------

def test_dedup_only_matches_same_mode(client, monkeypatch):
    calls = _engines(monkeypatch)
    data = _png("SAME")
    first = _post(client, None, data=data).json()
    assert first["mode"] == "auto" and not first.get("duplicate")
    # same bytes, same mode -> instant duplicate of the auto job
    again = _post(client, "auto", data=data).json()
    assert again["duplicate"] is True and again["job_id"] == first["job_id"]
    assert again["mode"] == "auto"
    # same bytes, Heavy -> a fresh job on the HEAVY route, not the auto one
    heavy = _post(client, "heavy", data=data).json()
    assert not heavy.get("duplicate") and heavy["job_id"] != first["job_id"]
    job = _done(client, _post(client, "heavy", data=data))
    assert job["job_id"] == heavy["job_id"] and job["mode"] == "heavy"
    assert job["result"]["pages"][0]["profile"] == "HEAVY"
    n_ocr = calls["gemini"] + calls["sarvam"]
    assert n_ocr == 2  # one auto run + one heavy run, dup hits cost nothing


# ---- migration ------------------------------------------------------------

def test_old_database_gets_mode_column(tmp_path, monkeypatch):
    db.DB_PATH = tmp_path / "old.db"
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, filename TEXT NOT NULL,"
        " sha256 TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT,"
        " created_at TEXT NOT NULL)")
    conn.execute("INSERT INTO jobs VALUES ('old1', 'a.png', 'x', 'pending', NULL,"
                 " '2026-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()
    db.init_db()
    db.init_db()  # idempotent
    assert db.get_job("old1")["mode"] == "auto"
    assert main._job_mode(db.get_job("old1")) == "auto"
    db.create_job("new1", "b.png", "y", "heavy")
    assert db.get_job("new1")["mode"] == "heavy"
