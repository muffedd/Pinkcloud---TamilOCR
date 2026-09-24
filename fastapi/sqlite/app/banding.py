"""Band extreme-aspect pages (palm-leaf manuscripts) before OCR.

CICT's Tirukkural palm leaves are very wide and very short (e.g. 4000x500,
1600x200 after the app's 1600px cap). Sent whole, Sarvam reads the squashed
leaf as a layout and returns garbage (hallucinated HTML table markup). So a
page whose long/short side ratio exceeds EXTREME_ASPECT is cut into
horizontal bands along its text lines, each band is OCR'd on its own through
the SAME route the page would use (same profile -> same engine order, same
job breaker, same Sarvam throttle, since every band goes through ocr_page),
and the bands' lines are stitched back into one page line list:

  * order: bands top to bottom, the engine's own order inside a band;
  * boxes: band-local -> prepared-page coordinates here, then the caller's
    preprocess Mapper takes them to the 1600px page as for any page;
  * overlap: bands overlap by a few px so no stroke is cut; a line that the
    neighbouring band also read is dropped from the band that does not own
    it (its centre is outside the band's core), and near-identical text on
    vertically overlapping lines from adjacent bands is kept once (higher
    confidence wins);
  * confidence: each line keeps its own engine confidence; the caller's
    score_ocr_lines then re-scores per line as for any page, so review
    routing is unchanged.

Normal pages never reach any of this: ocr_page_banded() calls the engine on
the whole page exactly as before.

Banding runs AFTER preprocess.prepare(): prepare crops the dark scan border,
deskews and flattens the yellowed background, and all three make the
line-projection profile below far more reliable (a skewed leaf would put
one text line across two bands). The prepare Mapper already maps boxes from
the prepared image back to the page, so bands only add a y-offset and an
optional upscale on top of it.

ponytail: every threshold is a named constant below; tune here.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass

import cv2
import numpy as np

from . import ocr as _ocr

logger = logging.getLogger("pinkcloud.banding")

# A page is "extreme" when max(w, h) / min(w, h) is above this. A4 is 1.41
# and a two-page book spread about 2.0; palm leaves run 5-10.
EXTREME_ASPECT = 2.5
# Hard cap on engine calls per page (Sarvam is throttled to SARVAM_RPM
# submissions per minute); more detected lines are grouped into this many.
MAX_BANDS = 8
# Tall pages (h > w): group lines into bands no taller than this x width.
TALL_BAND_ASPECT = 1.0
# Wide pages with no usable line profile: this many equal-height bands.
FALLBACK_BANDS_WIDE = 4
# Overlap added above and below each band's core, as a fraction of the
# median core height, never below BAND_OVERLAP_MIN_PX.
BAND_OVERLAP_FRAC = 0.15
BAND_OVERLAP_MIN_PX = 4
# Bands shorter than this are upscaled (at most BAND_MAX_UPSCALE) before OCR
# so a single capped palm-leaf line is not a 30px sliver.
BAND_MIN_HEIGHT = 64
BAND_MAX_UPSCALE = 2.0
# Adjacent-band duplicates: vertical overlap (of the shorter line) and text
# similarity at or above these mean "the same line read twice".
DUP_Y_OVERLAP = 0.5
DUP_TEXT_RATIO = 0.75
# Line detection: ink runs closer than MERGE_GAP_FRAC x median run height
# are one line; runs thinner than THIN_RUN_FRAC x median join a neighbour.
MERGE_GAP_FRAC = 0.35
THIN_RUN_FRAC = 0.5


@dataclass(frozen=True)
class Band:
    """One horizontal band. core_* tile the page with no gaps or overlap
    (they decide which band owns a line); y0/y1 is the padded crop."""

    core_y0: int
    core_y1: int
    y0: int
    y1: int


def is_extreme(shape) -> bool:
    h, w = int(shape[0]), int(shape[1])
    if h <= 0 or w <= 0:
        return False
    return max(w, h) / min(w, h) > EXTREME_ASPECT


def ink_profile(gray: np.ndarray) -> np.ndarray:
    """Fraction of ink pixels per row (Otsu split, lightly smoothed)."""
    h = gray.shape[0]
    ink = cv2.threshold(gray, 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    rows = ink.mean(axis=1).astype(np.float64)
    k = max(1, h // 100) | 1
    if k > 1 and rows.any():
        rows = np.convolve(rows, np.ones(k) / k, mode="same")
    return rows


def text_rows(gray: np.ndarray) -> list[tuple[int, int]]:
    """Text-line row ranges (y0, y1) from the horizontal ink profile."""
    h = gray.shape[0]
    rows = ink_profile(gray)
    if not rows.any():
        return []
    threshold = max(0.01, 0.5 * float(rows.mean()))
    min_h = 2
    runs: list[list[int]] = []
    inside, start = False, 0
    for y, v in enumerate(rows):
        if v > threshold and not inside:
            inside, start = True, y
        elif v <= threshold and inside:
            inside = False
            runs.append([start, y])
    if inside:
        runs.append([start, h])
    runs = [r for r in runs if r[1] - r[0] >= min_h]
    if not runs:
        return []
    # Tamil vowel signs / pulli sit a few px above or below the letter
    # body and show up as thin separate runs: merge small gaps, then fold
    # runs much thinner than a typical line into their nearest neighbour.
    med = float(np.median([b - a for a, b in runs]))
    merged: list[list[int]] = []
    for r in runs:
        if merged and r[0] - merged[-1][1] <= max(1, int(MERGE_GAP_FRAC * med)):
            merged[-1][1] = r[1]
        else:
            merged.append(list(r))
    med = float(np.median([b - a for a, b in merged]))
    i = 0
    while len(merged) > 1 and i < len(merged):
        a, b = merged[i]
        if b - a >= THIN_RUN_FRAC * med:
            i += 1
            continue
        up = a - merged[i - 1][1] if i > 0 else None
        down = merged[i + 1][0] - b if i + 1 < len(merged) else None
        j = i - 1 if down is None or (up is not None and up <= down) else i + 1
        lo, hi = min(i, j), max(i, j)
        merged[lo] = [merged[lo][0], merged[hi][1]]
        del merged[hi]
        i = 0
    return [(a, b) for a, b in merged]


def _group(cores: list[tuple[int, int]], n: int) -> list[tuple[int, int]]:
    """Merge consecutive cores into n roughly equal groups."""
    if len(cores) <= n:
        return cores
    edges = np.linspace(0, len(cores), n + 1).round().astype(int)
    return [(cores[a][0], cores[b - 1][1]) for a, b in zip(edges[:-1], edges[1:]) if b > a]


def plan_bands(gray: np.ndarray) -> list[Band]:
    """Split an extreme-aspect grayscale page into horizontal bands."""
    h, w = gray.shape[:2]
    lines = text_rows(gray)
    tall = h > w

    if len(lines) >= 2:
        # Cut at the emptiest row of each inter-line gap (centre of the
        # lowest plateau, so a stray vowel sign in the gap is not sliced):
        # one core per text line.
        rows = ink_profile(gray)
        cuts = [0]
        for i in range(1, len(lines)):
            g0, g1 = lines[i - 1][1], lines[i][0]
            seg = rows[g0:g1]
            if seg.size:
                low = np.flatnonzero(seg <= seg.min() + 1e-9)
                cuts.append(int(g0 + (low[0] + low[-1]) // 2))
            else:
                cuts.append(g0)
        cuts.append(h)
        cores = [(cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1)]
        if tall:
            # Tall page: group lines up to a near-square band (one line per
            # engine call would be slow and pointless on a tall page).
            limit = max(1, int(w * TALL_BAND_ASPECT))
            grouped: list[tuple[int, int]] = []
            for c in cores:
                if grouped and c[1] - grouped[-1][0] <= limit:
                    grouped[-1] = (grouped[-1][0], c[1])
                else:
                    grouped.append(c)
            cores = grouped
    else:
        # No usable line profile: fixed-height bands.
        if tall:
            step = max(1, int(w * TALL_BAND_ASPECT))
            n = max(1, -(-h // step))
        else:
            n = FALLBACK_BANDS_WIDE
        edges = np.linspace(0, h, n + 1).round().astype(int)
        cores = [(int(a), int(b)) for a, b in zip(edges[:-1], edges[1:]) if b > a]

    cores = _group(cores, MAX_BANDS)
    heights = sorted(b - a for a, b in cores)
    pad = max(BAND_OVERLAP_MIN_PX, int(round(BAND_OVERLAP_FRAC * heights[len(heights) // 2])))
    return [Band(a, b, max(0, a - pad), min(h, b + pad)) for a, b in cores]


def _is_stub(line: dict) -> bool:
    return str(line.get("body", "")).startswith("[stub]")


def _unknown_box(bbox) -> bool:
    return len(bbox) != 4 or bbox[2] <= 0 or bbox[3] <= 0


def _y_overlap(a, b) -> float:
    lo, hi = max(a[1], b[1]), min(a[1] + a[3], b[1] + b[3])
    shorter = min(a[3], b[3])
    return max(0, hi - lo) / shorter if shorter > 0 else 0.0


def _dedup(items: list[tuple[int, dict]], bands: list[Band]) -> list[tuple[int, dict]]:
    """Drop lines read twice in the overlap zone of adjacent bands."""
    drop: set[int] = set()
    for i, (bi, li) in enumerate(items):
        if _unknown_box(li["bbox"]) or _is_stub(li):
            continue
        band = bands[bi]
        cy = li["bbox"][1] + li["bbox"][3] / 2.0
        owned = band.core_y0 <= cy < band.core_y1 or (bi == len(bands) - 1 and cy >= band.core_y0)
        for j, (bj, lj) in enumerate(items):
            if j == i or j in drop or abs(bj - bi) != 1:
                continue
            if _unknown_box(lj["bbox"]) or _is_stub(lj):
                continue
            ov = _y_overlap(li["bbox"], lj["bbox"])
            if ov <= 0:
                continue
            if not owned and ov >= 0.3:
                # Neighbour band read the same strip and this band does not
                # own it: this copy is the overlap-zone echo.
                drop.add(i)
                break
            ratio = difflib.SequenceMatcher(None, li["body"], lj["body"]).ratio()
            if ov >= DUP_Y_OVERLAP and ratio >= DUP_TEXT_RATIO:
                ci, cj = float(li.get("confidence", 0)), float(lj.get("confidence", 0))
                if (ci, len(li["body"]), -i) < (cj, len(lj["body"]), -j):
                    drop.add(i)
                    break
    return [it for k, it in enumerate(items) if k not in drop]


def ocr_banded(img: np.ndarray, profile: str | None, ocr_fn) -> tuple[list[dict], float]:
    """OCR an extreme-aspect page band by band (see module docstring).

    Returns (lines, ms) in the same shape and coordinate space (`img`) as
    ocr_fn(img, profile). Also leaves ocr.page_engine() describing the
    page: the fallback engine if any band fell back, else the primary;
    "stub" only if every band failed."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    h, w = gray.shape[:2]
    bands = plan_bands(gray)
    logger.info("extreme-aspect page %dx%d: OCR in %d bands", w, h, len(bands))

    items: list[tuple[int, dict]] = []
    engines: list[str | None] = []
    primary: str | None = None
    total_ms = 0.0
    stub_bands = 0
    for bi, band in enumerate(bands):
        crop = img[band.y0:band.y1]
        ch = crop.shape[0]
        scale = min(BAND_MAX_UPSCALE, BAND_MIN_HEIGHT / ch) if ch < BAND_MIN_HEIGHT else 1.0
        if scale > 1.0:
            crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        crop = np.ascontiguousarray(crop)

        _ocr.reset_page_engine()
        lines, ms = ocr_fn(crop, profile)
        total_ms += float(ms or 0.0)
        eng, prim = _ocr.page_engine()
        engines.append(eng)
        primary = primary or prim

        if lines and all(_is_stub(ln) for ln in lines):
            stub_bands += 1
            # One marked line for the failed band (not the engine's two).
            items.append((bi, {**lines[0], "bbox": [int(w * 0.1), band.core_y0, int(w * 0.6),
                                                    max(1, band.core_y1 - band.core_y0)],
                               "confidence": 0.0}))
            continue
        for ln in lines:
            x, y, bw, bh = (float(v) for v in ln["bbox"])
            if _unknown_box([x, y, bw, bh]):
                bbox = [0, 0, 0, 0]
            else:
                x0, y0 = x / scale, y / scale + band.y0
                x1, y1 = (x + bw) / scale, (y + bh) / scale + band.y0
                x0, x1 = max(0.0, min(w, x0)), max(0.0, min(w, x1))
                y0, y1 = max(0.0, min(h, y0)), max(0.0, min(h, y1))
                bbox = [int(round(x0)), int(round(y0)),
                        max(1, int(round(x1 - x0))), max(1, int(round(y1 - y0)))]
            items.append((bi, {**ln, "bbox": bbox}))

    if stub_bands == len(bands):
        # Every band failed: the page gets the usual page-level stub.
        out = _ocr._stub_lines(img)
        page_eng = "stub"
    else:
        out = [ln for _bi, ln in _dedup(items, bands)]
        real = [e for e in engines if e and e != "stub"]
        fallback = [e for e in real if primary and e != primary]
        page_eng = fallback[0] if fallback else (real[0] if real else None)
    if any(e is not None for e in engines):
        _ocr._PAGE_ENGINE.engine = page_eng
        _ocr._PAGE_ENGINE.primary = primary
    return out, total_ms


def ocr_page_banded(img: np.ndarray, profile: str | None, ocr_fn) -> tuple[list[dict], float]:
    """ocr_fn(img, profile) for normal pages; banded OCR for extreme ones."""
    if not is_extreme(img.shape):
        return ocr_fn(img, profile)
    return ocr_banded(img, profile, ocr_fn)
