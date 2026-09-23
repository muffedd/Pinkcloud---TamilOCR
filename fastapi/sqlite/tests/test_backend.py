"""Reviewer test suite for the Pink Cloud backend.

Run from fastapi/sqlite/:
    python -m pytest tests/ -v

Works in two environments:
  - fresh venv WITHOUT paddle: stub-OCR tests run, real-OCR test skips
  - OCR venv WITH paddlepaddle 3.3.1 + paddleocr 3.7.0: everything runs
"""

import hashlib
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import db, storage
from app.main import app
from app.ocr import _parse_engine_results
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

def test_ocr_parses_paddle3_numpy_output():
    """rec_boxes arrives as NumPy ([x1,y1,x2,y2] rows, NumPy ints) —
    parsing must normalize to plain Python ints with no truthiness checks."""
    rec_texts = ["அகர முதல", "எழுத்தெல்லாம்"]
    rec_scores = [0.97, 0.88]
    rec_boxes = np.array([[10, 20, 300, 50], [12, 24, 310, 55]], dtype=np.int32)
    polys = None

    # simulate the exact paddle dict shape (None for rec_polys)
    results = [{"rec_texts": rec_texts, "rec_scores": rec_scores,
                "rec_boxes": rec_boxes, "rec_polys": polys}]

    lines = _parse_engine_results(results)
    assert len(lines) == 2
    assert lines[0]["body"] == "அகர முதல"
    assert lines[0]["confidence"] == pytest.approx(0.97)
    x, y, w, h = lines[0]["bbox"]
    assert (x, y, w, h) == (10, 20, 290, 30)
    # plain Python ints, not np.int32 (contract must JSON-serialize)
    assert type(x) is int and type(w) is int


def test_ocr_parse_polys_fallback():
    """When rec_boxes is None, 4-point polygons are used."""
    poly = np.array([[5, 6], [100, 4], [102, 40], [7, 42]], dtype=np.float32)
    lines = _parse_engine_results(
        [{"rec_texts": ["x"], "rec_scores": [0.9],
          "rec_boxes": None, "rec_polys": [poly]}])
    assert lines[0]["bbox"] == [5, 4, 97, 38]


def test_health_ok(client):
    """ok:true plus the OCR engine marker (stub output must be visible)."""
    r = client.get("/health").json()
    assert r["ok"] is True
    assert r["ocr_engine"] in {"paddle", "stub", "not_initialized"}


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
                         "chapter", "work", "manuscript_id", "script",
                         "material", "license", "doi"}
    assert set(page["lines"][0]) == {"id", "seq", "body", "bbox", "confidence"}


# --------------------------------------------------------------------------
# 7. real OCR end-to-end (skips when paddle is not installed)
# --------------------------------------------------------------------------

paddle_ok = True
try:
    # Detect WITHOUT importing in-process: on non-AVX CPUs importing
    # paddle hard-crashes the test process, so probe via subprocess.
    import subprocess as _sp
    import sys as _sys
    _p = _sp.run([_sys.executable, "-c", "import paddle"], capture_output=True)
    paddle_ok = _p.returncode == 0
except Exception:
    paddle_ok = False


@pytest.mark.skipif(not paddle_ok, reason="paddle not installed (stub mode)")
def test_real_scan_end_to_end_real_text(client, sample_files):
    """Full run through POST /jobs with the REAL PP-OCRv5 mobile models on
    a rendered Tamil page: the stored result must contain real Tamil
    text with high confidence, and /health must report the paddle engine."""
    assert client.get("/health").json()["ocr_engine"] == "paddle"

    # Render a clean Tamil page (Thirukkural 1) with Nirmala UI.
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1240, 800), (235, 235, 228))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype("C:/Windows/Fonts/Nirmala.ttf", 44)
    draw.text((140, 150), "அகர முதல எழுத்தெல்லாம் ஆதி", font=font, fill=(25, 25, 25))
    draw.text((140, 300), "பகவன் முதற்றே உலகு", font=font, fill=(25, 25, 25))
    p = sample_files["clean.png"].parent / "tamil_kural.png"
    img.save(str(p))

    r = _upload(client, p)
    assert r.status_code == 200, r.text
    job = client.get(f"/jobs/{r.json()['job_id']}").json()
    page = job["result"]["pages"][0]

    tamil = [ln["body"] for ln in page["lines"]
             if any("\u0b80" <= ch <= "\u0bff" for ch in ln["body"])]
    assert tamil, f"no Tamil text recognized: {page['lines']}"
    assert page["lines"][0]["confidence"] > 0.6
    print("\nREAL OCR TEXT:", tamil)
