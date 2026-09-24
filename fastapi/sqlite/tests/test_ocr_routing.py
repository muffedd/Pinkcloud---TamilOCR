"""FAST -> Gemini, HEAVY -> Sarvam routing. No network."""

import numpy as np

from app import ocr


def _img():
    return np.full((400, 300, 3), 255, dtype=np.uint8)


def test_fast_routes_to_gemini_and_heavy_to_sarvam(monkeypatch):
    seen = []
    monkeypatch.setattr(ocr, "_gemini_ocr",
                        lambda img, client=None: seen.append("gemini") or [])
    monkeypatch.setattr(ocr, "_sarvam_ocr",
                        lambda img, client=None: seen.append("sarvam") or [])

    ocr.ocr_page(_img(), "FAST")
    assert seen == ["gemini"]

    ocr.ocr_page(_img(), "HEAVY")
    assert seen == ["gemini", "sarvam"]


def test_fast_falls_back_to_sarvam_when_gemini_fails(monkeypatch):
    seen = []

    def boom(img, client=None):
        raise ocr.GeminiError("no key")

    monkeypatch.setattr(ocr, "_gemini_ocr", boom)
    monkeypatch.setattr(ocr, "_sarvam_ocr",
                        lambda img, client=None: seen.append("sarvam") or [])

    ocr.ocr_page(_img(), "FAST")
    assert seen == ["sarvam"]


def test_lines_with_boxes_cover_the_page_and_score_text():
    img = _img()
    lines = ocr._lines_with_boxes(img, ["அ", "ஆ", "இ"])
    assert len(lines) == 3
    for line in lines:
        x, y, w, h = line["bbox"]
        assert x >= 0 and y >= 0 and w > 0 and h > 0
        assert y + h <= img.shape[0] + 1
        assert 0.0 <= line["confidence"] <= 1.0


# ---- Gemini finishReason ----------------------------------------------------

import httpx  # noqa: E402
import pytest  # noqa: E402

_REAL_CLIENT = httpx.Client


def _gemini_client(finish, text="அகர முதல\nஎழுத்தெல்லாம்"):
    cand = {"content": {"parts": [{"text": text}]}}
    if finish is not None:
        cand["finishReason"] = finish

    def handler(request):
        return httpx.Response(200, json={"candidates": [cand]})

    return _REAL_CLIENT(transport=httpx.MockTransport(handler))


def test_gemini_stop_is_accepted(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    lines = ocr._gemini_ocr(_img(), client=_gemini_client("STOP"))
    assert [l["body"] for l in lines] == ["அகர முதல", "எழுத்தெல்லாம்"]


@pytest.mark.parametrize("finish", ["MAX_TOKENS", "SAFETY", "RECITATION",
                                    "OTHER", None])
def test_gemini_non_stop_raises_even_with_text(monkeypatch, finish):
    """Truncated / withheld output must not pass as a good page."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    with pytest.raises(ocr.GeminiError, match="finish"):
        ocr._gemini_ocr(_img(), client=_gemini_client(finish))


def test_gemini_max_tokens_falls_back_to_sarvam(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    # module state this test changes; monkeypatch restores it afterwards
    for name in ("_GEMINI_ERROR", "_SARVAM_ERROR", "_LAST_ENGINE"):
        monkeypatch.setattr(ocr, name, None)
    monkeypatch.setattr(httpx, "Client",
                        lambda *a, **k: _gemini_client("MAX_TOKENS"))
    sarvam = [{"body": "சர்வம்", "bbox": [0, 0, 10, 10], "confidence": 0.9}]
    monkeypatch.setattr(ocr, "_sarvam_ocr", lambda img, client=None: sarvam)
    lines, _ = ocr.ocr_page(_img(), "FAST")
    assert lines == sarvam
    assert "MAX_TOKENS" in ocr.engine_status()["gemini_error"]
    assert ocr.page_engine()[0] == "sarvam"
