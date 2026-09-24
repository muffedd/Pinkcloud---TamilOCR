"""Extreme-aspect (palm-leaf) pages are OCR'd in horizontal bands.

Synthetic leaves are PIL-generated: a yellowed strip with dark bars as text
lines, each bar a unique fraction of the page width so a fake engine can
name the line it sees regardless of crop offset or upscale. No network.

Run from fastapi/sqlite/:
    python -m pytest tests/test_palmleaf_banding.py -v
"""

import io

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app import banding, db, main, ocr, storage
from app.banding import Band, is_extreme, ocr_banded, ocr_page_banded, plan_bands

FONT = Path(__file__).resolve().parents[3] / "fonts" / "noto-sans-tamil.ttf"
WORDS = ["அகர", "முதல", "எழுத்தெல்லாம்", "ஆதி", "பகவன்"]
REPS = [2, 4, 6, 8, 10]                       # line i = "அகர முதல " x REPS[i]
FRACS: list[float] = []                       # ink width / page width per line


def _leaf(w=1600, h=200, n=5, bg=(205, 175, 115)):
    """Wide palm-leaf-like strip of real Tamil glyph lines (BGR array)
    and each line's true ink box. Line i is REPS[i % 5] words long, so its
    width fraction names it whatever the crop offset or upscale."""
    im = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(im)
    pitch = h / n
    font = ImageFont.truetype(str(FONT), max(8, int(pitch * 0.5)))
    boxes, fracs = [], []
    for i in range(n):
        text = " ".join(["அகர முதல"] * REPS[i % 5])
        x0 = int(w * 0.05)
        l, t, r, b = d.textbbox((0, 0), text, font=font)
        y0 = int(pitch * i + (pitch - (b - t)) / 2) - t
        d.text((x0, y0), text, font=font, fill=(35, 25, 20))
        bx0, by0, bx1, by1 = d.textbbox((x0, y0), text, font=font)
        boxes.append([bx0, by0, bx1 - bx0, by1 - by0])
        fracs.append((bx1 - bx0) / w)
    if n == 5 and not FRACS:
        FRACS.extend(fracs)
    return cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR), boxes


_leaf()  # fill FRACS


def _normal_page():
    im = Image.new("RGB", (1000, 1400), (240, 240, 240))
    d = ImageDraw.Draw(im)
    for i in range(6):
        d.rectangle([150, 200 + 150 * i, 850, 240 + 150 * i], fill=(20, 20, 20))
    return cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR)


def _ink_runs(crop):
    """Text rows in a crop -> (y0, y1, x0, x1): dark rows, with gaps
    shorter than a vowel-sign offset joined."""
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    ink = g < 110
    rows = ink.sum(axis=1) >= 2
    runs, y = [], 0
    while y < len(rows):
        if rows[y]:
            s = y
            while y < len(rows) and rows[y]:
                y += 1
            runs.append([s, y])
        else:
            y += 1
    joined = []
    for r in runs:
        if joined and r[0] - joined[-1][1] <= max(2, crop.shape[0] // 12):
            joined[-1][1] = r[1]
        else:
            joined.append(r)
    out = []
    for s, e in joined:
        cols = np.flatnonzero(ink[s:e].any(axis=0))
        out.append((s, e, int(cols[0]), int(cols[-1]) + 1))
    return out


class FakeEngine:
    """Reads every text row in the crop (clipped rows at the crop edge
    too, like a real engine echoing a neighbour line) and names it by its
    width fraction; confidence 0.5 + 0.1 x line index."""

    def __init__(self):
        self.calls = []

    def __call__(self, crop, profile):
        self.calls.append((crop.shape, profile))
        lines = []
        for y0, y1, x0, x1 in _ink_runs(crop):
            f = (x1 - x0) / crop.shape[1]
            k = int(np.argmin([abs(f - q) for q in FRACS]))
            lines.append({"body": WORDS[k], "bbox": [x0, y0, x1 - x0, y1 - y0],
                          "confidence": 0.5 + 0.1 * k})
        return lines, 1.0


# ---- detection ------------------------------------------------------------

def test_is_extreme_threshold():
    assert is_extreme((500, 4000))       # palm leaf, h x w
    assert is_extreme((200, 1600))
    assert is_extreme((4000, 500))       # tall strip
    assert not is_extreme((1400, 1000))  # A4-ish
    assert not is_extreme((1131, 1600))  # landscape A4
    assert not is_extreme((800, 1600))   # 2:1 spread
    assert banding.EXTREME_ASPECT == 2.5


def test_bands_follow_text_lines_and_tile_the_page():
    img, boxes = _leaf()
    bands = plan_bands(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    assert len(bands) == len(boxes)
    # cores tile [0, h) with no gap or overlap
    assert bands[0].core_y0 == 0 and bands[-1].core_y1 == img.shape[0]
    for a, b in zip(bands, bands[1:]):
        assert a.core_y1 == b.core_y0
    for band, (x, y, w, h) in zip(bands, boxes):
        # each band fully contains exactly its own line, plus some overlap
        assert band.y0 <= y and y + h <= band.y1
        assert band.y0 < band.core_y0 or band.core_y0 == 0
        assert band.y1 > band.core_y1 or band.core_y1 == img.shape[0]


def test_band_count_is_capped():
    img, _ = _leaf(w=3000, h=480, n=12)
    bands = plan_bands(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    assert len(bands) == banding.MAX_BANDS


def test_blank_leaf_falls_back_to_fixed_bands():
    blank = np.full((200, 1600), 220, np.uint8)
    bands = plan_bands(blank)
    assert len(bands) == banding.FALLBACK_BANDS_WIDE
    assert bands[0].core_y0 == 0 and bands[-1].core_y1 == 200


# ---- OCR + reassembly -----------------------------------------------------

def test_banded_ocr_orders_remaps_and_keeps_confidence():
    img, boxes = _leaf()
    eng = FakeEngine()
    lines, ms = ocr_banded(img, "HEAVY", eng)

    assert len(eng.calls) == len(boxes)
    assert all(p == "HEAVY" for _s, p in eng.calls)
    # thin bands were upscaled before OCR
    assert all(s[0] >= banding.BAND_MIN_HEIGHT - 1 for s, _p in eng.calls)
    assert [ln["body"] for ln in lines] == WORDS            # top-to-bottom
    for ln, (x, y, w, h), k in zip(lines, boxes, range(5)):
        bx, by, bw, bh = ln["bbox"]
        assert abs(bx - x) <= 2 and abs(by - y) <= 2, (ln, (x, y, w, h))
        assert abs(bw - w) <= 3 and abs(bh - h) <= 3, (ln, (x, y, w, h))
        assert ln["confidence"] == pytest.approx(0.5 + 0.1 * k)
    assert ms == pytest.approx(len(boxes))


def test_overlap_zone_echoes_are_deduplicated(monkeypatch):
    # Huge overlap: every band crop also sees its neighbours' lines.
    monkeypatch.setattr(banding, "BAND_OVERLAP_FRAC", 1.2)
    img, boxes = _leaf()
    raw = []

    def eng(crop, profile):
        out = FakeEngine()(crop, profile)
        raw.extend(out[0])
        return out

    lines, _ = ocr_banded(img, "FAST", eng)
    assert len(raw) > len(WORDS), "fixture must actually produce echoes"
    assert [ln["body"] for ln in lines] == WORDS
    for ln, (x, y, w, h) in zip(lines, boxes):
        assert abs(ln["bbox"][1] - y) <= 2


def test_line_split_across_fixed_bands_is_kept_once():
    bands = [Band(0, 100, 0, 110), Band(100, 200, 90, 200)]
    a = {"body": "அகர முதல", "bbox": [10, 92, 500, 16], "confidence": 0.6}
    b = {"body": "அகர முதல", "bbox": [10, 93, 500, 15], "confidence": 0.9}
    out = banding._dedup([(0, a), (1, b)], bands)
    assert len(out) == 1


def test_tall_strip_is_banded_top_to_bottom():
    im = Image.new("RGB", (400, 1600), (230, 230, 230))
    d = ImageDraw.Draw(im)
    font = ImageFont.truetype(str(FONT), 40)
    tops = []
    for i in range(10):
        d.text((30, 50 + 150 * i), "அகர முதல", font=font, fill=(20, 20, 20))
        tops.append(d.textbbox((30, 50 + 150 * i), "அகர முதல", font=font)[1])
    tall = cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR)
    eng = FakeEngine()
    lines, _ = ocr_banded(tall, None, eng)
    assert 2 <= len(eng.calls) <= banding.MAX_BANDS
    # bands are near-square, not one engine call per line
    assert all(s[0] <= 400 * banding.TALL_BAND_ASPECT * 1.5 for s, _p in eng.calls)
    ys = [ln["bbox"][1] for ln in lines]
    assert len(lines) == 10 and ys == sorted(ys)
    for y, t in zip(ys, tops):
        assert abs(y - t) <= 3


def test_all_bands_failing_gives_one_page_stub():
    img, _ = _leaf()

    def stub(crop, profile):
        return ocr._stub_lines(crop), 0.0

    lines, _ = ocr_banded(img, "HEAVY", stub)
    assert len(lines) == 2 and all(ln["body"].startswith("[stub]") for ln in lines)


def test_normal_page_calls_engine_once_on_the_whole_page():
    page = _normal_page()
    eng = FakeEngine()
    sentinel = [{"body": "x", "bbox": [1, 2, 3, 4], "confidence": 0.7}]

    def once(img, profile):
        eng.calls.append((img, profile))
        return sentinel, 5.0

    out = ocr_page_banded(page, "FAST", once)
    assert out == (sentinel, 5.0)
    assert len(eng.calls) == 1 and eng.calls[0][0] is page


# ---- through the real pipeline -------------------------------------------

@pytest.fixture()
def client(tmp_path):
    db.DB_PATH = tmp_path / "test.db"
    storage.UPLOAD_ROOT = tmp_path / "uploads"
    db.init_db()
    with TestClient(main.app, raise_server_exceptions=True) as c:
        yield c


def _png(img):
    return cv2.imencode(".png", img)[1].tobytes()


def _post(client, data, mode="auto"):
    r = client.post("/jobs", files={"file": ("leaf.png", data, "image/png")},
                    data={"mode": mode})
    assert r.status_code == 200, r.text
    job = client.get(f"/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "done", job
    return job["result"]["pages"][0]


def test_pipeline_bands_palm_leaf_in_page_coordinates(client, monkeypatch):
    big, _ = _leaf(w=3200, h=400)       # capped to 1600x200 by load_pages
    _, page_boxes = _leaf(w=1600, h=200)
    eng = FakeEngine()
    monkeypatch.setattr(main, "ocr_page", eng)
    page = _post(client, _png(big))
    assert len(eng.calls) == 5
    lines = page["lines"]
    assert [ln["body"] for ln in lines] == WORDS
    for ln, (x, y, w, h), k in zip(lines, page_boxes, range(5)):
        assert abs(ln["bbox"][0] - x) <= 3 and abs(ln["bbox"][1] - y) <= 3, ln
        assert ln["layout_confidence"] == pytest.approx(0.5 + 0.1 * k, abs=1e-3)
        assert "needs_review" in ln


def test_pipeline_heavy_mode_sends_every_band_to_sarvam(client, monkeypatch):
    img, _ = _leaf(w=1600, h=200, bg=(200, 172, 118))
    calls = {"sarvam": 0, "gemini": 0}

    def sarvam(crop, client=None):
        calls["sarvam"] += 1
        if calls["sarvam"] == 2:
            raise RuntimeError("band 2 failed")
        return FakeEngine()(crop, "HEAVY")[0]

    def gemini(crop, client=None):
        calls["gemini"] += 1
        return FakeEngine()(crop, "HEAVY")[0]

    monkeypatch.setattr(ocr, "_sarvam_ocr", sarvam)
    monkeypatch.setattr(ocr, "_gemini_ocr", gemini)
    page = _post(client, _png(img), mode="heavy")
    assert calls == {"sarvam": 5, "gemini": 1}   # band 2 fell back
    assert [ln["body"] for ln in page["lines"]] == WORDS
    assert page["ocr_engine"] == "gemini" and page["ocr_fallback"] is True


def test_pipeline_normal_page_is_not_banded(client, monkeypatch):
    seen = []

    def once(img, profile):
        seen.append(img.shape)
        return [{"body": "அகர", "bbox": [150, 200, 700, 40], "confidence": 0.9}], 1.0

    monkeypatch.setattr(main, "ocr_page", once)
    page = _post(client, _png(_normal_page()))
    assert len(seen) == 1 and seen[0][:2] == (1400, 1000)
    assert [ln["body"] for ln in page["lines"]] == ["அகர"]
