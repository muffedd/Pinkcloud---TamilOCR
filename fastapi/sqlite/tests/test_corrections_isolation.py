"""Corrections isolation: one job's saved corrections never show up in
another job's GET /corrections, export.txt, export.pdf or receipt.

Run from fastapi/sqlite/:
    python -m pytest tests/test_corrections_isolation.py -v

The GET tests run on main today. The export tests need the export routes
from slice/pipe-export-receipt and skip (with a reason) until it merges.
"""

import json

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import db, storage
from app.main import app

RAW = "வாழறிவன்"      # OCR word as recognized, same in both jobs
FIX_A = "வாலறிவன்"     # job A's reviewer fix
FIX_B = "வாலறிவனே"     # job B's reviewer fix (distinct, so leaks are visible)
LINE = "கற்றதனால் ஆய " + RAW

HAS_EXPORT = any(getattr(r, "path", "") == "/jobs/{job_id}/export.pdf"
                 for r in app.routes)
needs_export = pytest.mark.skipif(
    not HAS_EXPORT, reason="export routes not merged yet (slice/pipe-export-receipt)")


def _fix(after):
    return {"page": 1, "line": "L1", "word": 3, "before": RAW, "after": after}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("isolation")
    db.DB_PATH = tmp / "test.db"
    storage.UPLOAD_ROOT = tmp / "uploads"
    db.init_db()
    with TestClient(app) as c:
        yield c


_SEQ = 0


def _new_done_job(client, name):
    """Upload a page, then pin an identical known result on it so both
    jobs carry the same OCR word at (page 1, L1, word 3). The marker makes
    every upload's bytes unique: identical bytes dedup to the existing
    finished job, but these tests need two DISTINCT jobs."""
    global _SEQ
    _SEQ += 1
    img = np.full((600, 1200, 3), 240, np.uint8)
    cv2.putText(img, f"ISO-{_SEQ}", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (20, 20, 20), 2)
    ok, buf = cv2.imencode(".png", img)
    r = client.post("/jobs", files={"file": (name, buf.tobytes(), "image/png")})
    assert r.status_code == 200
    jid = r.json()["job_id"]
    page = {"page": 1, "profile": "FAST",
            "quality": {"blur": 1, "contrast": 1, "noise": 1, "skew_deg": 0},
            "lines": [{"id": "L1", "seq": 1, "body": LINE,
                       "bbox": [100, 100, 1000, 80], "confidence": 0.9}],
            "text": LINE, "needs_review": False, "processing_ms": 10}
    db.set_result(jid, "done", json.dumps({"pages": [page]}, ensure_ascii=False))
    return jid


@pytest.fixture()
def jobs(client):
    return _new_done_job(client, "a.png"), _new_done_job(client, "b.png")


def _get(client, jid):
    r = client.get(f"/jobs/{jid}/corrections")
    assert r.status_code == 200
    return r.json()


def _pdf_layer(client, jid):
    import pypdfium2 as pdfium
    r = client.get(f"/jobs/{jid}/export.pdf", params={"receipt_page": False})
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    pdf = pdfium.PdfDocument(r.content)
    return "".join(pdf[i].get_textpage().get_text_range() for i in range(len(pdf)))


def test_get_isolated(client, jobs):
    a, b = jobs
    assert client.put(f"/jobs/{a}/corrections",
                      json={"corrections": [_fix(FIX_A)]}).status_code == 200
    got_a, got_b = _get(client, a), _get(client, b)
    assert got_a["job_id"] == a and got_a["corrections"] == [_fix(FIX_A)]
    assert got_b == {"job_id": b, "corrections": [], "updated_at": None}


def test_each_job_keeps_its_own_map(client, jobs):
    a, b = jobs
    client.put(f"/jobs/{a}/corrections", json={"corrections": [_fix(FIX_A)]})
    client.put(f"/jobs/{b}/corrections", json={"corrections": [_fix(FIX_B)]})
    assert _get(client, a)["corrections"] == [_fix(FIX_A)]
    assert _get(client, b)["corrections"] == [_fix(FIX_B)]
    # Clearing A leaves B alone.
    client.put(f"/jobs/{a}/corrections", json={"corrections": []})
    assert _get(client, a)["corrections"] == []
    assert _get(client, b)["corrections"] == [_fix(FIX_B)]


def test_files_stay_in_own_upload_dir(client, jobs):
    a, b = jobs
    client.put(f"/jobs/{a}/corrections", json={"corrections": [_fix(FIX_A)]})
    assert (storage.UPLOAD_ROOT / a / "corrections.json").is_file()
    assert not (storage.UPLOAD_ROOT / b / "corrections.json").exists()
    assert not (storage.UPLOAD_ROOT / "corrections.json").exists()


# A literal ".." is left out: the client normalizes /jobs/../corrections to
# /corrections before routing, so it never reaches these routes at all.
@pytest.mark.parametrize("bad", [
    "%2e%2e", "A" * 32, "g" * 32, "0" * 31, "0" * 33,
])
def test_non_job_ids_never_reach_storage(client, bad):
    """Ids that are not 32 lowercase hex never read or write a file."""
    assert client.get(f"/jobs/{bad}/corrections").status_code == 404
    r = client.put(f"/jobs/{bad}/corrections", json={"corrections": [_fix(FIX_A)]})
    assert r.status_code == 404
    assert not (storage.UPLOAD_ROOT / "corrections.json").exists()


@needs_export
def test_export_txt_isolated(client, jobs):
    a, b = jobs
    client.put(f"/jobs/{a}/corrections", json={"corrections": [_fix(FIX_A)]})
    txt_a = client.get(f"/jobs/{a}/export.txt").text
    txt_b = client.get(f"/jobs/{b}/export.txt").text
    assert FIX_A in txt_a and RAW not in txt_a
    assert RAW in txt_b and FIX_A not in txt_b


@needs_export
def test_export_pdf_isolated(client, jobs):
    a, b = jobs
    client.put(f"/jobs/{a}/corrections", json={"corrections": [_fix(FIX_A)]})
    client.put(f"/jobs/{b}/corrections", json={"corrections": [_fix(FIX_B)]})
    layer_a, layer_b = _pdf_layer(client, a), _pdf_layer(client, b)
    assert FIX_A in layer_a and FIX_B not in layer_a and RAW not in layer_a
    assert FIX_B in layer_b and FIX_A not in layer_b and RAW not in layer_b


@needs_export
def test_receipt_counts_only_own_corrections(client, jobs):
    a, b = jobs
    client.put(f"/jobs/{a}/corrections", json={"corrections": [_fix(FIX_A)]})
    rc_a = client.get(f"/jobs/{a}/receipt").json()
    rc_b = client.get(f"/jobs/{b}/receipt").json()
    assert rc_a["job_id"] == a and rc_a["corrections"]["total"] == 1
    assert rc_b["job_id"] == b and rc_b["corrections"]["total"] == 0
