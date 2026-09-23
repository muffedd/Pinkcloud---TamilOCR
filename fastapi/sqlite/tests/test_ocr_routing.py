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
