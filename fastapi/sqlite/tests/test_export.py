"""Tests for the export/receipt slice (app/export.py + /jobs/{id}/export.*).

Run from fastapi/sqlite/:  python -m pytest tests/test_export.py -v
Works offline: the text-layer test feeds a hand-made page JSON.
"""

import hashlib

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import db, export, storage
from app.main import app

TAMIL_WORD = "வாலறிவன்"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("exportrun")
    db.DB_PATH = tmp / "test.db"
    storage.UPLOAD_ROOT = tmp / "uploads"
    db.init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def job_id(client, tmp_path_factory):
    img = np.full((800, 1200, 3), 235, np.uint8)
    cv2.putText(img, "TAMIL TEXT LINE", (100, 200), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (20, 20, 20), 3)
    ok, buf = cv2.imencode(".png", img)
    r = client.post("/jobs", files={"file": ("page.png", buf.tobytes(), "image/png")})
    assert r.status_code == 200
    return r.json()["job_id"]


def _page(lines):
    return {"page": 1, "profile": "FAST",
            "quality": {"blur": 1, "contrast": 1, "noise": 1, "skew_deg": 0},
            "lines": lines, "text": "\n".join(l["body"] for l in lines),
            "needs_review": False, "processing_ms": 10}


def test_pdf_text_layer_has_tamil_word(tmp_path):
    import pypdfium2 as pdfium
    img = np.full((600, 1200, 3), 240, np.uint8)
    master = tmp_path / "master.png"
    cv2.imwrite(str(master), img)
    page = _page([{"id": "L1", "seq": 1, "body": "கற்றதனால் ஆய பயனென்கொல் " + TAMIL_WORD,
                   "bbox": [100, 100, 1000, 80], "confidence": 0.9}])
    job = {"id": "x" * 32, "filename": "t.png", "sha256": "0" * 64,
           "status": "done", "created_at": "2026-01-01T00:00:00+00:00"}
    receipt = export.build_receipt(job, [page], reviewer="R", ocr_engine="sarvam")
    data = export.build_pdf(master, [page], receipt)
    assert data.startswith(b"%PDF")
    pdf = pdfium.PdfDocument(data)
    assert len(pdf) == 2  # visible text page + scan page, no receipt page
    assert TAMIL_WORD in pdf[0].get_textpage().get_text_range()  # visible text
    assert TAMIL_WORD in pdf[1].get_textpage().get_text_range()  # scan layer
    for i in range(len(pdf)):
        assert "0" * 64 not in pdf[i].get_textpage().get_text_range()
    assert pdf.get_metadata_dict()["Keywords"].startswith("master-sha256:" + "0" * 64)


RECEIPT_MARKERS = ("Pink Cloud processing receipt", "processing receipt",
                   "Master SHA-256", "Job:", "Reviewer:", "Processing time")

POEM = ["அகர முதல எழுத்தெல்லாம் ஆதி",
        "பகவன் முதற்றே உலகு",
        "கற்றதனால் ஆய பயனென்கொல் " + TAMIL_WORD]


def _real_scan_job(client, lines, name="50823516_poem_44.jpg"):
    """Upload a real image, then store known OCR lines as the job result."""
    import json
    img = np.full((1200, 1600, 3), 235, np.uint8)
    cv2.putText(img, "SCAN", (200, 300), cv2.FONT_HERSHEY_SIMPLEX, 4, (20, 20, 20), 8)
    ok, buf = cv2.imencode(".jpg", img)
    jid = client.post("/jobs", files={"file": (name, buf.tobytes(), "image/jpeg")}).json()["job_id"]
    page = _page([{"id": f"L{i}", "seq": i, "body": b,
                   "bbox": [100, 100 + i * 70, 1200, 60], "confidence": 0.95}
                  for i, b in enumerate(lines, 1)])
    db.set_result(jid, "done", json.dumps({"pages": [page]}, ensure_ascii=False))
    return jid


def _ink(pdf_page) -> float:
    """Share of non-white pixels when the page is rendered."""
    g = np.asarray(pdf_page.render(scale=1).to_pil().convert("L"))
    return float((g < 200).mean())


def test_export_pdf_page1_is_visible_ocr_text_no_receipt(client):
    """Regression (user report): the PDF must open with his document's
    recognized text, visibly, and carry no receipt page at all."""
    import pypdfium2 as pdfium
    jid = _real_scan_job(client, POEM)
    r = client.get(f"/jobs/{jid}/export.pdf")
    assert r.status_code == 200
    pdf = pdfium.PdfDocument(r.content)
    assert len(pdf) == 2  # text page, scan page
    first = pdf[0].get_textpage().get_text_range()
    for line in POEM:
        assert line in first, line
    assert _ink(pdf[0]) > 0.003  # glyphs are actually drawn, not an invisible layer
    # the visible text is real vector glyphs (paths), the scan is page 2
    kinds0 = {o.type for o in pdf[0].get_objects()}
    assert pdfium.raw.FPDF_PAGEOBJ_PATH in kinds0
    assert pdfium.raw.FPDF_PAGEOBJ_IMAGE not in kinds0
    assert pdfium.raw.FPDF_PAGEOBJ_IMAGE in {o.type for o in pdf[1].get_objects()}
    assert all(line in pdf[1].get_textpage().get_text_range() for line in POEM)
    for i in range(len(pdf)):
        text = pdf[i].get_textpage().get_text_range()
        for marker in RECEIPT_MARKERS:
            assert marker not in text, (i, marker)
    assert "PinkCloudReceipt" not in pdf.get_metadata_dict()
    # old links with ?receipt_page=true still get no receipt page
    old = pdfium.PdfDocument(client.get(f"/jobs/{jid}/export.pdf",
                                        params={"receipt_page": True}).content)
    assert len(old) == 2
    # the receipt data itself is still served by its own endpoint
    rc = client.get(f"/jobs/{jid}/receipt")
    assert rc.status_code == 200 and rc.json()["lines"]["total"] == len(POEM)


def test_export_pdf_long_text_flows_onto_more_text_pages_in_order(client):
    import pypdfium2 as pdfium
    lines = [f"{i:03d} " + POEM[i % 3] for i in range(90)]
    jid = _real_scan_job(client, lines, name="long.jpg")
    pdf = pdfium.PdfDocument(client.get(f"/jobs/{jid}/export.pdf").content)
    assert len(pdf) >= 3  # >=2 text pages + the scan
    text_pages = len(pdf) - 1
    visible = "\n".join(pdf[i].get_textpage().get_text_range() for i in range(text_pages))
    pos = [visible.index(l) for l in lines]
    assert pos == sorted(pos)  # reading order kept across page breaks
    assert pdfium.raw.FPDF_PAGEOBJ_IMAGE in {o.type for o in pdf[len(pdf) - 1].get_objects()}


def test_receipt_counts(client, job_id):
    r = client.get(f"/jobs/{job_id}/receipt", params={"reviewer": "Anu"})
    assert r.status_code == 200
    rc = r.json()
    assert rc["page_count"] == 1
    assert rc["reviewer"] == "Anu"
    assert rc["lines"]["auto"] + rc["lines"]["human_review"] == rc["lines"]["total"]
    assert rc["pages"]["auto"] + rc["pages"]["human_review"] == 1
    master = storage.master_path(job_id)
    assert rc["master"]["sha256"] == hashlib.sha256(master.read_bytes()).hexdigest()
    assert rc["master"]["verified_on_disk"] is True


def test_export_endpoints(client, job_id):
    p = client.get(f"/jobs/{job_id}/export.pdf")
    assert p.status_code == 200 and p.content.startswith(b"%PDF")
    assert p.headers["content-type"] == "application/pdf"
    t = client.get(f"/jobs/{job_id}/export.txt")
    assert t.status_code == 200 and "Master SHA-256" in t.text and "=== page 1 ===" in t.text


def test_unknown_job_404(client):
    for path in ("receipt", "export.pdf", "export.txt", "export.docx"):
        assert client.get(f"/jobs/{'f' * 32}/{path}").status_code == 404


def test_tamil_filename_export_no_500(client):
    """Non-ASCII upload names must not crash the latin-1 download headers."""
    img = np.full((400, 600, 3), 235, np.uint8)
    ok, buf = cv2.imencode(".png", img)
    r = client.post("/jobs", files={"file": ("தமிழ்.png", buf.tobytes(), "image/png")})
    assert r.status_code == 200
    jid = r.json()["job_id"]
    for path in ("export.txt", "export.pdf", "export.docx"):
        resp = client.get(f"/jobs/{jid}/{path}")
        assert resp.status_code == 200
        cd = resp.headers["content-disposition"]
        assert 'filename="pinkcloud.' in cd and "filename*=UTF-8''" in cd


def test_saved_corrections_applied_to_exports(client):
    import json
    import pypdfium2 as pdfium
    img = np.full((600, 1200, 3), 240, np.uint8)
    ok, buf = cv2.imencode(".png", img)
    jid = client.post("/jobs", files={"file": ("c.png", buf.tobytes(), "image/png")}).json()["job_id"]
    raw, fixed = "வாழறிவன்", TAMIL_WORD
    page = _page([{"id": "L1", "seq": 1, "body": "கற்றதனால் ஆய " + raw,
                   "bbox": [100, 100, 1000, 80], "confidence": 0.9}])
    db.set_result(jid, "done", json.dumps({"pages": [page]}, ensure_ascii=False))
    cpath = storage.UPLOAD_ROOT / jid / "corrections.json"

    cpath.write_text("{not json", encoding="utf-8")  # malformed -> raw OCR
    assert raw in client.get(f"/jobs/{jid}/export.txt").text

    cpath.write_text(json.dumps({"corrections": [
        {"page": 1, "line": "L1", "word": 3, "before": raw, "after": fixed},
        {"page": 1, "line": "L1", "word": 1, "before": "stale", "after": "X"},  # skipped
    ]}, ensure_ascii=False), encoding="utf-8")
    txt = client.get(f"/jobs/{jid}/export.txt").text
    assert fixed in txt and raw not in txt and "கற்றதனால்" in txt
    pdf = pdfium.PdfDocument(client.get(f"/jobs/{jid}/export.pdf").content)
    layer = pdf[0].get_textpage().get_text_range()
    assert fixed in layer and raw not in layer
    rc = client.get(f"/jobs/{jid}/receipt").json()
    assert rc["corrections"]["total"] == 1 and rc["corrections"]["by_tier"] == {"human": 1}


# --------------------------------------------------------------------------
# DOCX export
# --------------------------------------------------------------------------

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _docx_text(data: bytes) -> str:
    import io
    import zipfile
    from docx import Document
    assert zipfile.is_zipfile(io.BytesIO(data))
    assert "word/document.xml" in zipfile.ZipFile(io.BytesIO(data)).namelist()
    return "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)


def test_export_docx_endpoint(client, job_id):
    r = client.get(f"/jobs/{job_id}/export.docx", params={"reviewer": "Anu"})
    assert r.status_code == 200
    assert r.headers["content-type"] == DOCX_TYPE
    assert r.content[:2] == b"PK"
    assert 'filename="page.docx"' in r.headers["content-disposition"]
    sha = hashlib.sha256(storage.master_path(job_id).read_bytes()).hexdigest()
    assert r.headers["x-master-sha256"] == sha
    text = _docx_text(r.content)
    assert "Page 1" in text and "Processing receipt" in text
    assert sha in text
    assert "Reviewer: Anu" in text


def test_export_docx_corrections_applied(client):
    import json
    img = np.full((600, 1200, 3), 240, np.uint8)
    ok, buf = cv2.imencode(".png", img)
    jid = client.post("/jobs", files={"file": ("d.png", buf.tobytes(), "image/png")}).json()["job_id"]
    raw, fixed = "வாழறிவன்", TAMIL_WORD
    page = _page([{"id": "L1", "seq": 1, "body": "கற்றதனால் ஆய " + raw,
                   "bbox": [100, 100, 1000, 80], "confidence": 0.9}])
    db.set_result(jid, "done", json.dumps({"pages": [page]}, ensure_ascii=False))
    assert raw in _docx_text(client.get(f"/jobs/{jid}/export.docx").content)
    (storage.UPLOAD_ROOT / jid / "corrections.json").write_text(json.dumps({"corrections": [
        {"page": 1, "line": "L1", "word": 3, "before": raw, "after": fixed},
    ]}, ensure_ascii=False), encoding="utf-8")
    text = _docx_text(client.get(f"/jobs/{jid}/export.docx").content)
    assert fixed in text and raw not in text and "கற்றதனால்" in text
    assert "Corrections: 1" in text
    # receipt is the final section
    assert text.index("Processing receipt") > text.index(fixed)


def test_export_docx_unfinished_job_409(client):
    img = np.full((400, 600, 3), 235, np.uint8)
    ok, buf = cv2.imencode(".png", img)
    jid = client.post("/jobs", files={"file": ("q.png", buf.tobytes(), "image/png")}).json()["job_id"]
    db.set_result(jid, "processing", None)
    assert client.get(f"/jobs/{jid}/export.docx").status_code == 409


def test_export_docx_unknown_job_404(client):
    assert client.get(f"/jobs/{'e' * 32}/export.docx").status_code == 404
