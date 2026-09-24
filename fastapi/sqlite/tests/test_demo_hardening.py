"""Demo hardening: per-page OCR failure isolation (both engines), upload
size/page caps, Tamil filename on the PDF receipt page, no "[stub]" lines
in txt/docx bodies, and visible corrections problems.

Run from fastapi/sqlite/:
    python -m pytest tests/test_demo_hardening.py -v
"""

import io

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import db, export, main, ocr, storage
from app.main import app

TAMIL_NAME = "தமிழ்_ஆவணம்.png"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("hardening")
    db.DB_PATH = tmp / "test.db"
    storage.UPLOAD_ROOT = tmp / "uploads"
    db.init_db()
    with TestClient(app) as c:
        yield c


def _pdf_bytes(n: int) -> bytes:
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument.new()
    for _ in range(n):
        pdf.new_page(595, 842)
    buf = io.BytesIO()
    pdf.save(buf)
    pdf.close()
    return buf.getvalue()


def _png_bytes() -> bytes:
    ok, buf = cv2.imencode(".png", np.full((300, 600, 3), 240, np.uint8))
    return buf.tobytes()


def _upload_pdf(client, n=3):
    r = client.post("/jobs", files={"file": ("doc.pdf", _pdf_bytes(n), "application/pdf")})
    assert r.status_code == 200, r.text
    return client.get(f"/jobs/{r.json()['job_id']}").json()


def _fail_on_call(n, real):
    calls = {"n": 0}

    def fn(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == n:
            raise RuntimeError("engine blew up sk-secret-123")
        return real(*args, **kwargs)
    return fn


def _check_page2_failed(job):
    assert job["status"] == "done"
    pages = job["result"]["pages"]
    assert [p["page"] for p in pages] == [1, 2, 3]
    bad = pages[1]
    assert bad["needs_review"] is True
    assert bad["lines"] and bad["lines"][0]["body"].startswith("[stub] OCR failed on this page")
    assert bad["lines"][0]["confidence"] == 0.0
    assert "sk-secret-123" not in bad["lines"][0]["body"]  # key scrubbed


# ---- F4: per-page OCR error isolation ------------------------------------

def test_sarvam_failure_on_one_page_keeps_job(client, monkeypatch):
    """Sarvam raising on page 2 only: page 2 is a marked [stub] page, pages
    1 and 3 keep their OCR, the job is done."""
    monkeypatch.setenv("SARVAM_API_KEY", "sk-secret-123")
    ok = lambda img: [{"body": "சரி", "bbox": [1, 1, 10, 10], "confidence": 0.9}]
    monkeypatch.setattr(ocr, "_sarvam_ocr", _fail_on_call(2, ok))
    job = _upload_pdf(client)
    assert job["status"] == "done"
    pages = job["result"]["pages"]
    assert [p["page"] for p in pages] == [1, 2, 3]
    assert pages[0]["lines"][0]["body"] == "சரி"
    assert pages[2]["lines"][0]["body"] == "சரி"
    bad = pages[1]
    assert bad["needs_review"] is True
    assert all(ln["body"].startswith("[stub]") and ln["confidence"] == 0.0
               for ln in bad["lines"])
    assert "sk-secret-123" not in str(client.get("/health").json())  # key scrubbed


def test_sarvam_raising_past_ocr_page_keeps_job(client, monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "sk-secret-123")
    monkeypatch.setattr(main, "ocr_page", _fail_on_call(
        2, lambda img, profile=None: (ocr._stub_lines(img), 1.0)))
    _check_page2_failed(_upload_pdf(client))


# ---- F2: upload caps -----------------------------------------------------

def _job_count():
    import sqlite3
    con = sqlite3.connect(db.DB_PATH)
    try:
        return con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    finally:
        con.close()


def test_caps_are_named_constants():
    assert main.MAX_UPLOAD_BYTES == 50 * 1024 * 1024
    assert main.MAX_PDF_PAGES == 25


def test_pdf_over_page_cap_rejected_before_job(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_PDF_PAGES", 2)
    before = _job_count()
    r = client.post("/jobs", files={"file": ("d.pdf", _pdf_bytes(3), "application/pdf")})
    assert r.status_code == 413
    assert "at most 2 pages" in r.json()["detail"]
    assert _job_count() == before
    r = client.post("/jobs", files={"file": ("d.pdf", _pdf_bytes(2), "application/pdf")})
    assert r.status_code == 200


def test_oversized_single_upload_rejected(client, monkeypatch):
    data = _png_bytes()
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", len(data) - 1)
    monkeypatch.setattr(main, "_MULTIPART_SLACK_BYTES", 10 ** 9)  # exercise the read cap
    before = _job_count()
    r = client.post("/jobs", files={"file": ("p.png", data, "image/png")})
    assert r.status_code == 413
    assert _job_count() == before


def test_oversized_request_rejected_by_content_length(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 100)
    monkeypatch.setattr(main, "_MULTIPART_SLACK_BYTES", 0)
    r = client.post("/jobs", files={"file": ("p.png", _png_bytes(), "image/png")})
    assert r.status_code == 413
    assert "too large" in r.json()["detail"]


def test_multi_image_total_size_capped(client, monkeypatch):
    data = _png_bytes()
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", int(len(data) * 1.5))
    monkeypatch.setattr(main, "_MULTIPART_SLACK_BYTES", 10 ** 9)
    files = [("files", (f"p{i}.png", data, "image/png")) for i in range(2)]
    assert client.post("/jobs", files=files).status_code == 413
    one = [("files", ("p.png", data, "image/png"))]
    assert client.post("/jobs", files=one).status_code == 200


# ---- F7: Tamil filename, PDF export (no receipt page any more) ----------

def test_pdf_tamil_filename_no_receipt_page(tmp_path):
    """The PDF has no receipt page; an empty job still opens with a visible
    note (not a blank page) and the Tamil name only goes in the metadata."""
    import pypdfium2 as pdfium
    master = tmp_path / "m.png"
    cv2.imwrite(str(master), np.full((400, 600, 3), 240, np.uint8))
    page = {"page": 1, "profile": "FAST", "quality": {}, "lines": [],
            "text": "", "needs_review": True, "processing_ms": 1}
    job = {"id": "y" * 32, "filename": TAMIL_NAME, "sha256": "0" * 64,
           "status": "done", "created_at": "2026-01-01T00:00:00+00:00"}
    receipt = export.build_receipt(job, [page])
    pdf = pdfium.PdfDocument(export.build_pdf(master, [page], receipt))
    assert len(pdf) == 2  # note page + scan
    assert export.EMPTY_TEXT_NOTE in pdf[0].get_textpage().get_text_range()
    for i in range(len(pdf)):
        text = pdf[i].get_textpage().get_text_range()
        assert "File:" not in text and "receipt" not in text.lower()
    assert TAMIL_NAME in pdf.get_metadata_dict()["Title"]


# ---- F6: no "[stub]" lines in txt / docx bodies ----------------------------

def _stub_page():
    lines = [{"id": "L1", "seq": 1, "body": "[stub] OCR unavailable", "bbox": [0, 0, 1, 1], "confidence": 0.0},
             {"id": "L2", "seq": 2, "body": "உண்மை வரி", "bbox": [0, 5, 1, 1], "confidence": 0.9}]
    return {"page": 1, "profile": "FAST", "quality": {}, "lines": lines,
            "text": "\n".join(l["body"] for l in lines), "needs_review": True,
            "processing_ms": 1}


def _job():
    return {"id": "z" * 32, "filename": "t.png", "sha256": "0" * 64,
            "status": "done", "created_at": "2026-01-01T00:00:00+00:00"}


def test_txt_body_drops_stub_lines():
    p = _stub_page()
    receipt = export.build_receipt(_job(), [p])
    txt = export.build_txt([p], receipt)
    assert txt == "உண்மை வரி\n"  # transcribed text only, no receipt header
    assert receipt["ocr"]["stub_pages"] == 1  # the receipt still reports it


def test_docx_body_drops_stub_lines():
    from docx import Document
    p = _stub_page()
    doc = Document(io.BytesIO(export.build_docx([p], export.build_receipt(_job(), [p]))))
    texts = [para.text for para in doc.paragraphs]
    assert "உண்மை வரி" in texts
    assert not any(t.startswith("[stub]") for t in texts)


# ---- F5 + F8: corrections robustness -------------------------------------

@pytest.fixture
def png_job(client):
    r = client.post("/jobs", files={"file": ("p.png", _png_bytes(), "image/png")})
    assert r.status_code == 200
    return r.json()["job_id"]


def test_get_corrections_non_object_json_is_clean_500(client, png_job):
    path = storage.UPLOAD_ROOT / png_job / "corrections.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    r = client.get(f"/jobs/{png_job}/corrections")
    assert r.status_code == 500
    assert r.json()["detail"] == "corrections unreadable"


@pytest.mark.parametrize("content", ["{not json", "\"a string\"", "{\"corrections\": 5}"])
def test_damaged_corrections_visible_in_receipt(client, png_job, content):
    (storage.UPLOAD_ROOT / png_job / "corrections.json").write_text(content, encoding="utf-8")
    rc = client.get(f"/jobs/{png_job}/receipt").json()
    assert rc["corrections_error"]
    # the problem is reported by the receipt only; the TXT export carries
    # nothing but the transcribed text
    txt = client.get(f"/jobs/{png_job}/export.txt").text
    assert "WARNING" not in txt and not txt.startswith("#")


def test_malformed_entries_reported(client, png_job):
    (storage.UPLOAD_ROOT / png_job / "corrections.json").write_text(
        '{"corrections": [{"page": 1}, {"page": 1, "line": "L1", "word": 1, "after": "x"}]}',
        encoding="utf-8")
    rc = client.get(f"/jobs/{png_job}/receipt").json()
    assert rc["corrections_error"] == "1 of 2 saved corrections were malformed and skipped"


def test_no_corrections_file_no_error(client, png_job):
    rc = client.get(f"/jobs/{png_job}/receipt").json()
    assert rc["corrections_error"] is None
    assert "WARNING" not in client.get(f"/jobs/{png_job}/export.txt").text


def test_preprocess_crash_falls_back_to_raw_page(client, monkeypatch):
    """prepare() raising on a page must not sink the job: the page is OCR'd
    on the raw 1600px image with an identity mapper, job still done."""
    seen = []

    def boom(img):
        raise RuntimeError("cv2 exploded")

    def fake_ocr(img, profile=None):
        seen.append(img)
        return ([{"body": "சரி", "bbox": [1, 1, 10, 10], "confidence": 0.9}], 1.0)

    monkeypatch.setattr(main, "prepare", boom)
    monkeypatch.setattr(main, "ocr_page", fake_ocr)
    job = _upload_pdf(client)
    assert job["status"] == "done"
    pages = job["result"]["pages"]
    assert [p["page"] for p in pages] == [1, 2, 3]
    assert all(p["lines"][0]["body"] == "சரி" for p in pages)
    assert len(seen) == 3 and all(getattr(img, "ndim", 0) == 3 for img in seen)
