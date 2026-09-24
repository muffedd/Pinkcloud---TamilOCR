"""Reviewer test suite for the Pink Cloud backend.

Run from fastapi/sqlite/:
    python -m pytest tests/ -v

Never calls the network: tests/conftest.py drops SARVAM_API_KEY, so
end-to-end runs get the marked [stub] OCR lines (Sarvam itself is covered
with a mocked transport in tests/test_sarvam_ocr.py).
"""

import hashlib
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import db, storage
from app.main import app
from app.pdfutil import load_pages, to_gray
from app.router import choose_profile, compute_scores
from app.schema_out import build_page_result


# --------------------------------------------------------------------------
# fixtures: synthetic pages + clients
# --------------------------------------------------------------------------

def _text_page(width=1200, height=1600, ink=20, bg=235):
    """A clean synthetic 'text page' (BGR)."""
    img = np.full((height, width), bg, np.uint8)
    for y in range(150, height - 100, 60):
        cv2.putText(img, "TAMIL TEXT LINE", (120, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.2, ink, 3)
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """TestClient with the DB redirected to a temp file."""
    tmp = tmp_path_factory.mktemp("jobrun")
    db.DB_PATH = tmp / "test.db"
    storage.UPLOAD_ROOT = tmp / "uploads"
    db.init_db()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    try:
        db.DB_PATH.unlink()
    except PermissionError:
        pass  # Windows keeps the file mapped while a connection lingers


@pytest.fixture(scope="module")
def sample_files(tmp_path_factory):
    """clean.png, damaged.jpg, one.pdf, two.pdf, multi.tif on disk."""
    tmp = tmp_path_factory.mktemp("samples")
    files = {}

    # clean page
    clean = _text_page()
    p = tmp / "clean.png"
    cv2.imwrite(str(p), clean)
    files["clean.png"] = p

    # damaged page: blurred + grainy + low contrast
    gray = cv2.cvtColor(_text_page(ink=120, bg=190), cv2.COLOR_BGR2GRAY)
    damaged = cv2.GaussianBlur(gray, (15, 15), 6)
    noise = np.random.default_rng(7).normal(0, 18, damaged.shape).astype(np.float32)
    damaged = np.clip(damaged.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    p = tmp / "damaged.jpg"
    cv2.imwrite(str(p), damaged, [cv2.IMWRITE_JPEG_QUALITY, 60])
    files["damaged.jpg"] = p

    # PDFs (1-page and 2-page) via pypdfium2 writer
    import pypdfium2 as pdfium
    for name, n in (("one.pdf", 1), ("two.pdf", 2)):
        pdf = pdfium.PdfDocument.new()
        for _ in range(n):
            pdf.new_page(595, 842)
        p = tmp / name
        pdf.save(str(p))
        pdf.close()
        files[name] = p

    # 2-page TIFF via Pillow
    from PIL import Image
    a = Image.fromarray(cv2.cvtColor(_text_page(1200, 800), cv2.COLOR_BGR2RGB))
    b = Image.fromarray(cv2.cvtColor(_text_page(1000, 700), cv2.COLOR_BGR2RGB))
    p = tmp / "multi.tif"
    a.save(str(p), save_all=True, append_images=[b], compression="tiff_deflate")
    files["multi.tif"] = p

    # corrupt png: valid name/type, garbage bytes
    p = tmp / "corrupt.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\nthis is not really a png")
    files["corrupt.png"] = p

    # evil.exe: will be uploaded with a spoofed image/png content type
    p = tmp / "evil.exe"
    p.write_bytes(b"MZ\x90\x00fake binary")
    files["evil.exe"] = p

    return files


def _upload(client, path: Path, content_type: str | None = None):
    ctype = content_type or {
        ".png": "image/png", ".jpg": "image/jpeg", ".pdf": "application/pdf",
        ".tif": "image/tiff", ".tiff": "image/tiff", ".exe": "application/octet-stream",
    }[path.suffix.lower()]
    with open(path, "rb") as f:
        return client.post("/jobs",
                           files={"file": (path.name, f, ctype)})


def _post_and_get(client, path: Path, content_type=None):
    r = _upload(client, path, content_type)
    assert r.status_code == 200, r.text
    job = client.get(f"/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "done", job
    return job


# --------------------------------------------------------------------------
# 1. dependencies / PDF support
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name,count", [("one.pdf", 1), ("two.pdf", 2)],
                         ids=["one.pdf-1", "two.pdf-2"])
def test_pdf_job(client, sample_files, name, count):
    """PDF uploads must render (bitmap.to_pil needs pinned Pillow)."""
    job = _post_and_get(client, sample_files[name])
    pages = job["result"]["pages"]
    assert len(pages) == count
    assert [p["page"] for p in pages] == list(range(1, count + 1))


def test_multipage_tiff_keeps_all_pages(client, sample_files):
    """A 2-page TIFF must yield 2 pages (cv2.imreadmulti, not imdecode)."""
    job = _post_and_get(client, sample_files["multi.tif"])
    assert len(job["result"]["pages"]) == 2


# --------------------------------------------------------------------------
# 2. OCR wiring
# --------------------------------------------------------------------------

def test_health_ok(client):
    """ok:true plus the OCR engine marker (stub output must be visible)."""
    r = client.get("/health").json()
    assert r["ok"] is True
    assert r["ocr_engine"] in {"gemini", "sarvam", "stub"}
    assert r["ocr_engine_selected"] == "sarvam"


# --------------------------------------------------------------------------
# 3. upload validation
# --------------------------------------------------------------------------

def test_spoofed_ext_rejected_with_400(client, sample_files):
    """evil.exe with a spoofed image/png type -> 400 (BOTH checks matter)."""
    r = _upload(client, sample_files["evil.exe"], content_type="image/png")
    assert r.status_code == 400


def test_corrupt_image_rejected_with_4xx(client, sample_files):
    """Valid name/type but undecodable bytes -> 4xx, NOT a failed job."""
    r = _upload(client, sample_files["corrupt.png"])
    assert 400 <= r.status_code < 500
    assert r.json()["detail"]  # explains the decode failure


def test_valid_png_still_processes(client, sample_files):
    """Valid files still process end-to-end."""
    job = _post_and_get(client, sample_files["clean.png"])
    assert job["result"]["pages"][0]["profile"] in {"FAST", "HEAVY"}


# --------------------------------------------------------------------------
# 4. router: skew + FAST/HEAVY thresholds
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [("clean.png", "FAST"),
                                           ("damaged.jpg", "HEAVY")],
                         ids=["clean.png", "damaged.jpg"])
def test_router_badge(client, sample_files, name, expected):
    gray = to_gray(load_pages(sample_files[name])[0])
    profile, _ = choose_profile(compute_scores(gray))
    assert profile == expected


def test_router_skew_detects_rotation(sample_files):
    """A page rotated by 8 degrees must read skew_deg ~= 8 (not 0)."""
    img = _text_page()
    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), 8.0, 1.0)
    # 3-channel borderValue: a scalar fills the out-of-canvas region BLACK
    # on some cv2 builds, which breaks Otsu + skew detection downstream.
    rotated = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_LINEAR,
                             borderValue=(235, 235, 235))
    scores = compute_scores(cv2.cvtColor(rotated, cv2.COLOR_BGR2GRAY))
    assert 6.0 <= scores.skew_deg <= 10.0, f"skew_deg={scores.skew_deg}"


def test_clean_page_badge_under_1s(sample_files):
    """Badge decision (metrics + route) must take well under 1 second."""
    import time
    gray = to_gray(load_pages(sample_files["clean.png"])[0])
    t0 = time.perf_counter()
    profile, _ = choose_profile(compute_scores(gray))
    elapsed = time.perf_counter() - t0
    assert profile == "FAST"
    assert elapsed < 1.0, f"badge took {elapsed:.3f}s"


# --------------------------------------------------------------------------
# 5. master integrity
# --------------------------------------------------------------------------

def test_master_sha256_unchanged(client, sample_files):
    """Recorded hash == hash of the bytes we sent == hash of file on disk."""
    path = sample_files["clean.png"]
    data = path.read_bytes()
    r = _upload(client, path)
    jid = r.json()["job_id"]
    job = client.get(f"/jobs/{jid}").json()
    assert job["sha256"] == hashlib.sha256(data).hexdigest()
    assert storage.verify_master(jid, job["sha256"])


# --------------------------------------------------------------------------
# 6. schema_out: needs_review + reading order
# --------------------------------------------------------------------------

def test_stub_page_flagged_for_review():
    """Zero-confidence stub lines must set needs_review=true, even on a
    FAST profile."""
    stub_lines = [{"body": "[stub] ocr unavailable",
                   "bbox": [100, 300, 600, 40], "confidence": 0.0},
                  {"body": "[stub] ocr unavailable",
                   "bbox": [100, 500, 600, 40], "confidence": 0.0}]
    page = build_page_result(1, "FAST",
                             {"blur": 900, "contrast": 0.8, "noise": 4,
                              "skew_deg": 0},
                             stub_lines, 5.0)
    assert page["needs_review"] is True


def test_real_confidence_not_flagged():
    """High-confidence real OCR on a FAST page -> no review flag."""
    lines = [{"body": "அகர முதல எழுத்தெல்லாம் ஆதி",
              "bbox": [180, 320, 1240, 90], "confidence": 0.975}]
    page = build_page_result(1, "FAST",
                             {"blur": 900, "contrast": 0.8, "noise": 4,
                              "skew_deg": 0},
                             lines, 5000.0)
    assert page["needs_review"] is False


def test_reading_order_same_band():
    """Right-hand box 3px higher must NOT sort before the left box."""
    lines = [
        {"body": "LEFT", "bbox": [100, 100, 300, 40], "confidence": 0.9},
        {"body": "RIGHT", "bbox": [500, 97, 300, 40], "confidence": 0.9},
    ]
    page = build_page_result(1, "FAST",
                             {"blur": 900, "contrast": 0.8, "noise": 4,
                              "skew_deg": 0},
                             lines, 1.0)
    assert [l["body"] for l in page["lines"]] == ["LEFT", "RIGHT"]


def test_page_result_matches_frozen_contract():
    """Only schema-defined keys, exact line shape (additionalProperties:false)."""
    page = build_page_result(1, "FAST",
                             {"blur": 1, "contrast": 1, "noise": 1,
                              "skew_deg": 1},
                             [{"body": "x", "bbox": [0, 0, 1, 1],
                               "confidence": 0.9}], 1.0)
    assert set(page) <= {"page", "profile", "quality", "lines", "text",
                         "preprocessed", "words", "corrections", "verdicts",
                         "needs_review", "processing_ms", "specimen",
                         "suggestions",
                         "chapter", "work", "manuscript_id", "script",
                         "material", "license", "doi"}
    # slice 2 added per-line needs_review (additive; schema.json updated)
    assert set(page["lines"][0]) == {"id", "seq", "body", "bbox", "confidence",
                                     "needs_review"}


# --------------------------------------------------------------------------
# 7. no Sarvam key end-to-end: marked stub page, never fake text
# --------------------------------------------------------------------------

def test_no_key_end_to_end_is_marked_stub(client, sample_files):
    """Without SARVAM_API_KEY (conftest drops it) the job still completes;
    every line is a marked [stub] line at confidence 0, the page needs
    review, and /health says stub (not a real engine)."""
    job = _post_and_get(client, sample_files["clean.png"])
    page = job["result"]["pages"][0]
    assert page["lines"]
    assert all(ln["body"].startswith("[stub]") for ln in page["lines"])
    assert all(ln["confidence"] == 0.0 for ln in page["lines"])
    assert page["needs_review"] is True
    h = client.get("/health").json()
    assert h["ocr_engine"] == "stub" and h["sarvam_key_set"] is False


# --------------------------------------------------------------------------
# 6. multi-image jobs: repeated `files` field -> one job, one page per image
# --------------------------------------------------------------------------

def _sized_page(width, height):
    """A text page with a distinctive size so page order is checkable."""
    return _text_page(width=width, height=height)


@pytest.fixture(scope="module")
def multi_images(tmp_path_factory):
    """Three images of different formats AND sizes (png, jpg, webp)."""
    tmp = tmp_path_factory.mktemp("multi")
    specs = [("a.png", 900, 1200, "image/png"),
             ("b.jpg", 1000, 700, "image/jpeg"),
             ("c.webp", 800, 800, "image/webp")]
    out = []
    for name, w, h, ctype in specs:
        p = tmp / name
        assert cv2.imwrite(str(p), _sized_page(w, h))
        out.append((p, ctype, (h, w)))
    return out


def _upload_many(client, items):
    handles = [open(p, "rb") for p, _, _ in items]
    try:
        return client.post("/jobs", files=[
            ("files", (p.name, fh, ctype))
            for (p, ctype, _), fh in zip(items, handles)])
    finally:
        for fh in handles:
            fh.close()


def test_multi_image_job_n_pages_in_order(client, multi_images):
    """N images -> N pages, page k is image k (checked via page PNG size)."""
    r = _upload_many(client, multi_images)
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    job = client.get(f"/jobs/{jid}").json()
    assert job["status"] == "done", job
    pages = job["result"]["pages"]
    assert [p["page"] for p in pages] == [1, 2, 3]
    assert job["filename"] == "a.png"
    for n, (_, _, shape) in enumerate(multi_images, start=1):
        img = client.get(f"/jobs/{jid}/pages/{n}/image")
        assert img.status_code == 200
        dec = cv2.imdecode(np.frombuffer(img.content, np.uint8), cv2.IMREAD_COLOR)
        assert dec.shape[:2] == shape, f"page {n} is not image {n}"
    assert client.get(f"/jobs/{jid}/pages/4/image").status_code == 404


def test_multi_image_masters_hashed_in_order(client, multi_images):
    """Each image stored byte-for-byte; job sha256 = combined hash."""
    r = _upload_many(client, multi_images)
    jid = r.json()["job_id"]
    job = client.get(f"/jobs/{jid}").json()
    digests = [hashlib.sha256(p.read_bytes()).hexdigest()
               for p, _, _ in multi_images]
    assert job["sha256"] == storage.combined_sha256(digests)
    masters = storage.master_paths(jid)
    assert [m.name for m in masters] == [
        "master-001.png", "master-002.jpg", "master-003.webp"]
    for m, (p, _, _) in zip(masters, multi_images):
        assert m.read_bytes() == p.read_bytes()
    assert storage.verify_master(jid, job["sha256"])
    # tampering with any page breaks verification
    masters[1].write_bytes(masters[1].read_bytes() + b"x")
    assert not storage.verify_master(jid, job["sha256"])


def test_multi_image_exports_span_all_pages(client, multi_images):
    r = _upload_many(client, multi_images)
    jid = r.json()["job_id"]
    rec = client.get(f"/jobs/{jid}/receipt").json()
    assert rec["page_count"] == 3
    assert rec["master"]["verified_on_disk"] is True
    assert rec["master"]["files"] == [
        "master-001.png", "master-002.jpg", "master-003.webp"]
    assert client.get(f"/jobs/{jid}/export.txt").status_code == 200
    pdf = client.get(f"/jobs/{jid}/export.pdf")
    assert pdf.status_code == 200
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf.content)
    # visible text page(s) first, then the 3 scans; no receipt page
    assert len(doc) == 4
    assert all(pdfium.raw.FPDF_PAGEOBJ_IMAGE in {o.type for o in doc[i].get_objects()}
               for i in (1, 2, 3))
    doc.close()


def test_multi_image_corrections_address_page_n(client, multi_images):
    r = _upload_many(client, multi_images)
    jid = r.json()["job_id"]
    body = {"corrections": [{"page": 3, "line": "L1", "word": 1,
                             "before": "x", "after": "y"}]}
    assert client.put(f"/jobs/{jid}/corrections", json=body).status_code == 200
    got = client.get(f"/jobs/{jid}/corrections").json()["corrections"]
    assert got[0]["page"] == 3


def test_multi_image_rejects_pdf_and_bad_members(client, multi_images, sample_files):
    good = multi_images[:1]
    pdf = [(sample_files["one.pdf"], "application/pdf", None)]
    r = _upload_many(client, good + pdf)
    assert r.status_code == 400 and "file 2" in r.json()["detail"]
    bad = [(sample_files["corrupt.png"], "image/png", None)]
    r = _upload_many(client, good + bad)
    assert r.status_code == 422 and "file 2" in r.json()["detail"]
    evil = [(sample_files["evil.exe"], "image/png", None)]
    assert _upload_many(client, evil).status_code == 400


def test_file_and_files_together_rejected(client, multi_images, sample_files):
    p, ctype, _ = multi_images[0]
    with open(p, "rb") as a, open(p, "rb") as b:
        r = client.post("/jobs", files=[("file", (p.name, a, ctype)),
                                        ("files", (p.name, b, ctype))])
    assert r.status_code == 400


def test_no_file_is_422(client):
    assert client.post("/jobs").status_code == 422


def test_single_webp_upload(client, multi_images):
    """.webp is now accepted on the single-file path too."""
    p, ctype, _ = multi_images[2]
    job = _post_and_get(client, p, content_type=ctype)
    assert len(job["result"]["pages"]) == 1
    assert storage.master_paths(job["job_id"])[0].name == "master.webp"


def test_one_file_in_files_list(client, multi_images):
    r = _upload_many(client, multi_images[:1])
    assert r.status_code == 200
    job = client.get(f"/jobs/{r.json()['job_id']}").json()
    assert len(job["result"]["pages"]) == 1
