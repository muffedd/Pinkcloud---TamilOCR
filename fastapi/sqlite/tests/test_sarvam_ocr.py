"""Sarvam Document AI engine: mocked HTTP only, no network, no real key.

Response shapes follow Sarvam's docs (digitise -> status -> download-url ->
ZIP with metadata/page_NNN.json) and the page JSON the team captured from a
real Sarvam run (ocr_outputs/sarvam/raw/sample1/json/sample1.json).
"""

import io
import json
import zipfile

import httpx
import numpy as np
import pytest

from app import ocr
from app.schema_out import build_page_result

FAKE_KEY = "test-key-not-real"

PAGE = {
    "page_num": 1,
    "image_width": 1008,
    "image_height": 1324,
    "blocks": [
        {"block_id": "p1-b2", "coordinates": {"x1": 120, "y1": 42, "x2": 867, "y2": 200},
         "layout_tag": "paragraph", "confidence": 0.8628, "reading_order": 2,
         "text": "எ - து, தலைவன்\nலும், தலைவி\nகின்ற அன்னதொரு\nகூறாநிற்பது."},
        {"block_id": "p1-b1", "coordinates": {"x1": 373, "y1": 0, "x2": 868, "y2": 42},
         "layout_tag": "paragraph", "confidence": 0.3003, "reading_order": 1,
         "text": "தகவிசொல்லியது,"},
        {"block_id": "p1-b9", "coordinates": {"x1": 10, "y1": 10, "x2": 20, "y2": 20},
         "layout_tag": "image", "confidence": 0.9, "reading_order": 3, "text": "  \n "},
    ],
}


def _zip(page=PAGE) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("page.png/page.md", "# md")
        zf.writestr("page.png/metadata/page_001.json", json.dumps(page, ensure_ascii=False))
        zf.writestr("manifest.json", json.dumps({"status": "completed"}))
    return buf.getvalue()


class FakeSarvam:
    """httpx.MockTransport handler that records every request."""

    def __init__(self, statuses=("pending", "running", "completed"), fail=None, zip_bytes=None):
        self.statuses = list(statuses)
        self.fail = fail or {}
        self.zip_bytes = zip_bytes if zip_bytes is not None else _zip()
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        for key, resp in list(self.fail.items()):
            if key in path and resp:
                return resp.pop(0)
        if path.endswith("/doc-ai/v1/job/digitise"):
            return httpx.Response(201, json={"job_id": "job-1", "status": "pending", "run_id": "r1"})
        if path.endswith("/status"):
            st = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return httpx.Response(200, json={"job_id": "job-1", "status": st, "pipeline": "digitise",
                                             "usage": {}, "created_at": "", "updated_at": ""})
        if path.endswith("/download-url"):
            return httpx.Response(200, json={"method": "GET", "url": "https://blob.example/out.zip",
                                             "expires_at": "", "headers": {"x-ms-blob-type": "BlockBlob"}})
        if request.url.host == "blob.example":
            return httpx.Response(200, content=self.zip_bytes)
        return httpx.Response(404)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", FAKE_KEY)
    monkeypatch.setenv("SARVAM_POLL_S", "0")
    monkeypatch.setenv("SARVAM_TIMEOUT_S", "30")
    monkeypatch.delenv("OCR_ENGINE", raising=False)
    monkeypatch.delenv("SARVAM_FALLBACK", raising=False)
    monkeypatch.setattr(ocr.time, "sleep", lambda s: None)
    monkeypatch.setattr(ocr, "_LAST_ENGINE", None)
    monkeypatch.setattr(ocr, "_SARVAM_ERROR", None)
    return monkeypatch


def _img(w=1008, h=1324):
    return np.full((h, w, 3), 255, dtype=np.uint8)


def _patch_client(monkeypatch, fake):
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: real(transport=httpx.MockTransport(fake)))


# ---- parsing ---------------------------------------------------------------

def test_parse_blocks_split_into_lines_in_reading_order():
    lines = ocr._parse_sarvam_page(PAGE, 1008, 1324)
    assert [l["body"] for l in lines] == [
        "தகவிசொல்லியது,", "எ - து, தலைவன்", "லும், தலைவி", "கின்ற அன்னதொரு", "கூறாநிற்பது.",
    ]
    assert lines[0]["bbox"] == [373, 0, 495, 42]
    # 4 lines share the block box: same x/w, stacked evenly over y 42..200
    ys = [l["bbox"][1] for l in lines[1:]]
    assert ys == sorted(ys) and ys[0] == 42
    assert all(l["bbox"][0] == 120 and l["bbox"][2] == 747 for l in lines[1:])
    assert sum(l["bbox"][3] for l in lines[1:]) == 158
    # every line inherits its block's (layout) confidence
    assert lines[0]["confidence"] == pytest.approx(0.3003)
    assert all(l["confidence"] == pytest.approx(0.8628) for l in lines[1:])


def test_parse_rescales_to_our_image_and_uses_bbox_norm():
    page = {"image_width": 2000, "image_height": 1000, "blocks": [
        {"coordinates": {"x1": 200, "y1": 100, "x2": 1200, "y2": 200}, "confidence": 0.5, "text": "a"},
        {"bbox_norm": [0.5, 0.5, 0.75, 0.6], "confidence": 7, "text": "b"},
    ]}
    a, b = ocr._parse_sarvam_page(page, 1000, 500)
    assert a["bbox"] == [100, 50, 500, 50]
    assert b["bbox"] == [500, 250, 250, 50]
    assert b["confidence"] == 1.0  # clamped to contract range


def test_parse_output_passes_contract_builder():
    lines = ocr._parse_sarvam_page(PAGE, 1008, 1324)
    page = build_page_result(1, "FAST", {"blur": 1, "contrast": 1, "noise": 1, "skew_deg": 0},
                             lines, 10.0)
    assert [l["id"] for l in page["lines"]] == ["L1", "L2", "L3", "L4", "L5"]
    assert page["text"].startswith("தகவிசொல்லியது,\nஎ - து")
    for l in page["lines"]:
        # slice 2 added per-line needs_review (additive; schema.json updated)
        assert set(l) == {"id", "seq", "body", "bbox", "confidence", "needs_review"}
        assert len(l["bbox"]) == 4 and 0 <= l["confidence"] <= 1


def test_page_json_from_zip_missing_metadata():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", "{}")
    with pytest.raises(ocr.SarvamError):
        ocr._page_json_from_zip(buf.getvalue())


# ---- HTTP flow -------------------------------------------------------------

def test_ocr_page_sarvam_full_flow(env):
    fake = FakeSarvam()
    _patch_client(env, fake)
    lines, ms = ocr.ocr_page(_img())
    assert len(lines) == 5 and ms >= 0
    assert ocr.engine_status()["ocr_engine"] == "sarvam"

    create = fake.calls[0]
    assert create.method == "POST" and create.url.path == "/doc-ai/v1/job/digitise"
    assert create.headers["api-subscription-key"] == FAKE_KEY
    body = create.content
    assert b'name="language"' in body and b"ta-IN" in body
    assert b'name="output_format"' in body and b"md" in body
    assert b'filename="page.png"' in body
    assert [c.url.path for c in fake.calls[1:4]] == ["/doc-ai/v1/job/job-1/status"] * 3
    # presigned download: Sarvam's headers only, never our API key
    blob = fake.calls[-1]
    assert blob.url.host == "blob.example"
    assert "api-subscription-key" not in blob.headers
    assert blob.headers["x-ms-blob-type"] == "BlockBlob"


def test_retries_429_then_succeeds(env):
    fake = FakeSarvam(fail={"/digitise": [httpx.Response(429, json={"error": {}})]})
    _patch_client(env, fake)
    lines, _ = ocr.ocr_page(_img())
    assert len(lines) == 5
    assert [c.url.path for c in fake.calls[:2]] == ["/doc-ai/v1/job/digitise"] * 2


def _is_stub(lines):
    return bool(lines) and all(
        l["body"].startswith("[stub]") and l["confidence"] == 0.0 for l in lines)


def test_failed_job_returns_marked_stub(env):
    fake = FakeSarvam(statuses=("failed",))
    _patch_client(env, fake)
    lines, _ = ocr.ocr_page(_img())
    assert _is_stub(lines)
    st = ocr.engine_status()
    assert st["ocr_engine"] == "stub"
    assert "failed" in st["sarvam_error"]
    assert FAKE_KEY not in json.dumps(st)


def test_http_401_returns_stub_and_error_is_visible(env):
    fake = FakeSarvam(fail={"/digitise": [httpx.Response(401, text="invalid key")]})
    _patch_client(env, fake)
    lines, _ = ocr.ocr_page(_img())
    assert _is_stub(lines)
    assert "401" in ocr.engine_status()["sarvam_error"]


def test_rejected_job_returns_marked_stub(env):
    fake = FakeSarvam(statuses=("rejected",))
    _patch_client(env, fake)
    lines, _ = ocr.ocr_page(_img())
    assert _is_stub(lines)
    assert ocr.engine_status()["ocr_engine"] == "stub"


def test_missing_key_returns_stub_without_network(env):
    env.delenv("SARVAM_API_KEY")
    fake = FakeSarvam()
    _patch_client(env, fake)
    assert ocr.engine_status()["ocr_engine"] == "stub"  # before any page
    lines, _ = ocr.ocr_page(_img())
    assert _is_stub(lines) and fake.calls == []
    st = ocr.engine_status()
    assert st["ocr_engine"] == "stub"
    assert st["ocr_engine_selected"] == "sarvam" and st["sarvam_key_set"] is False
    assert "SARVAM_API_KEY" in st["ocr_error"]


def test_status_before_any_page_with_key_is_sarvam(env):
    st = ocr.engine_status()
    assert st["ocr_engine"] == "sarvam" and st["sarvam_key_set"] is True
    assert "ocr_error" not in st


@pytest.mark.parametrize("name,value", [("OCR_ENGINE", "paddle"),
                                        ("SARVAM_FALLBACK", "paddle")])
def test_removed_paddle_settings_are_ignored(env, name, value):
    """Old .env values can't route around Sarvam or revive a local engine."""
    env.setenv(name, value)
    fake = FakeSarvam()
    _patch_client(env, fake)
    lines, _ = ocr.ocr_page(_img())
    assert len(lines) == 5 and fake.calls  # Sarvam was called
    assert ocr.engine_status()["ocr_engine"] == "sarvam"


@pytest.mark.parametrize("name", ["OCR_ENGINE", "SARVAM_FALLBACK"])
def test_removed_paddle_settings_still_fall_back_to_stub(env, name):
    env.setenv(name, "paddle")
    _patch_client(env, FakeSarvam(statuses=("failed",)))
    assert _is_stub(ocr.ocr_page(_img())[0])


def test_removed_paddle_settings_log_warning(env, caplog):
    env.setenv("OCR_ENGINE", "paddle")
    with caplog.at_level("WARNING", logger="pinkcloud.ocr"):
        ocr.warn_legacy_env()
    assert "OCR_ENGINE=paddle is ignored" in caplog.text
    env.setenv("OCR_ENGINE", "sarvam")
    caplog.clear()
    ocr.warn_legacy_env()
    assert caplog.text == ""


def test_timeout_raises_and_returns_stub(env):
    env.setenv("SARVAM_TIMEOUT_S", "0")
    env.setenv("SARVAM_POLL_S", "1")
    fake = FakeSarvam(statuses=("running",))
    _patch_client(env, fake)
    assert _is_stub(ocr.ocr_page(_img())[0])
    assert "still 'running'" in ocr.engine_status()["sarvam_error"]


def test_no_paddle_left_in_engine():
    import pathlib
    src = pathlib.Path(ocr.__file__).read_text(encoding="utf-8")
    assert "import paddle" not in src and "from paddleocr" not in src
    assert not hasattr(ocr, "_paddle_ocr") and not hasattr(ocr, "_get_engine")


def test_key_is_never_hardcoded():
    import pathlib
    src = pathlib.Path(ocr.__file__).read_text(encoding="utf-8")
    assert 'os.environ.get("SARVAM_API_KEY")' in src
    assert "sk_" not in src
