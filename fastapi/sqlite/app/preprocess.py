"""Safe-wins OCR preprocessing for Pink Cloud.

Applied to a COPY of the page before OCR. Four operations, all
information-preserving by construction:

  1. crop dark scan borders            (geometry)
  2. deskew                            (geometry)
  3. grayscale background flattening   (photometric: kills yellowing/shadows,
                                        keeps pulli dots and thin strokes)
  4. upscale only when the scan is low-res (geometry)

Heavy preprocessing (denoise, binarize, AI restoration) is deliberately NOT
here: Sarvam/Gemini are modern vision models that clean up internally, and
this repo's own measurement showed aggressive repair HURTS clean pages
(raw+filter CER 27.22% vs repaired+filter 31.91%).

The app's bbox contract is the uncropped page image, so prepare() also
returns a Mapper that puts OCR bboxes back on the original page.

ponytail: the crop/deskew/upscale heuristics are simple on purpose; each
constant below is the one place to tune if a real page disagrees.
"""

from __future__ import annotations

import cv2
import numpy as np

DARK = 60            # pixel value below which a pixel counts as "scan border"
DARK_FRAC = 0.97     # a border row/col is >=97% dark
MAX_TRIM = 0.12      # never trim more than 12% from any side
DESKEW_MIN = 0.2     # degrees; smaller angles are not worth rotating
LOW_RES_SIDE = 1000  # px; a page whose long side is below this is low-res
UPSCALE_TO = 1600    # px; long-side target after upscaling (the app's cap)
MAX_UPSCALE = 2.0    # never upscale more than 2x
FLATTEN_SIDE = 900   # px; background-estimate resolution


def _content_box(gray: np.ndarray) -> tuple[int, int, int, int]:
    """Trim rows/cols at the edges that are almost entirely dark.

    Conservative by design: only walks inward from each edge while the
    line is >= DARK_FRAC dark, and refuses the crop entirely if it would
    remove more than MAX_TRIM of the page (a genuinely dark document must
    not lose its text)."""
    h, w = gray.shape
    dark = gray < DARK
    rows = dark.mean(axis=1)
    cols = dark.mean(axis=0)

    top = 0
    while top < h * MAX_TRIM and rows[top] >= DARK_FRAC:
        top += 1
    bottom = h
    while bottom > h * (1 - MAX_TRIM) and rows[bottom - 1] >= DARK_FRAC:
        bottom -= 1
    left = 0
    while left < w * MAX_TRIM and cols[left] >= DARK_FRAC:
        left += 1
    right = w
    while right > w * (1 - MAX_TRIM) and cols[right - 1] >= DARK_FRAC:
        right -= 1

    if right - left < w * 0.3 or bottom - top < h * 0.3:
        return 0, 0, w, h  # too aggressive -> leave the page alone
    return left, top, right, bottom


def _deskew_angle(gray: np.ndarray) -> float:
    """Projection-profile deskew: coarse 0.5 deg, then fine 0.1 deg.

    Higher profile sharpness = straighter text lines. A best score within
    2% of the 0 deg score means the page is already straight, so it is
    left untouched (same guard the repair pipeline uses)."""
    s = min(1.0, 1000 / max(gray.shape))
    small = (cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
             if s < 1 else gray)
    bw = (small < 128).astype(np.uint8)
    if not bw.any():
        return 0.0

    h, w = bw.shape
    center = (w / 2.0, h / 2.0)

    def score(angle: float) -> float:
        m = cv2.getRotationMatrix2D(center, angle, 1.0)
        rot = cv2.warpAffine(bw, m, (w, h), flags=cv2.INTER_NEAREST)
        profile = rot.sum(axis=1, dtype=np.float64)
        return float(np.sum(np.diff(profile) ** 2))

    base = score(0.0)
    coarse = np.arange(-5.0, 5.001, 0.5)
    a0 = float(coarse[int(np.argmax([score(a) for a in coarse]))])
    fine = np.arange(a0 - 0.5, a0 + 0.501, 0.1)
    scores = [score(a) for a in fine]
    best = float(fine[int(np.argmax(scores))])
    return 0.0 if max(scores) <= 1.02 * base else best


def _flatten(gray: np.ndarray) -> np.ndarray:
    """Divide out the paper background, then stretch.

    The background is estimated at low resolution with a morphological
    close, so only large-scale shading (yellowing, stains, shadows) is
    removed; small dark marks stay dark."""
    h, w = gray.shape
    s = min(1.0, FLATTEN_SIDE / max(h, w))
    small = (cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
             if s < 1 else gray.copy())
    k = max(3, (min(small.shape) // 25) | 1)
    bg = cv2.morphologyEx(small, cv2.MORPH_CLOSE,
                          cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    bg = cv2.GaussianBlur(bg, (k, k), 0)
    bg = cv2.resize(bg, (w, h), interpolation=cv2.INTER_LINEAR)
    bg = bg.astype(np.float32) + 1.0
    norm = np.clip(gray.astype(np.float32) / bg * 255.0, 0.0, 255.0)
    lo, hi = np.percentile(norm, (1, 99))
    if hi - lo < 8:
        return gray.copy()  # no usable contrast (blank page) -> leave as-is
    hi = max(hi, lo + 1.0)
    return np.clip((norm - lo) / (hi - lo) * 255.0, 0.0, 255.0).astype(np.uint8)


def _upscale_factor(gray: np.ndarray) -> float:
    """1.0 unless the page is low-res (long side below LOW_RES_SIDE).

    ponytail: long side is a robust proxy for scan resolution; glyph-height
    estimates on Tamil pages are noisy because marks break into tiny
    components. Raise UPSCALE_TO if a specific scan needs more."""
    longest = max(gray.shape)
    if longest >= LOW_RES_SIDE:
        return 1.0
    return min(MAX_UPSCALE, UPSCALE_TO / longest)


class Mapper:
    """Maps a bbox from the prepared image back to the original page.

    The transform chain is: original -> crop (offset) -> rotate (about the
    crop centre) -> resize (scale). to_page() applies the inverse in the
    opposite order, then takes the axis-aligned bbox of the 4 corners."""

    def __init__(self, offset, angle, scale, crop_shape):
        self.ox, self.oy = offset
        self.angle = float(angle)
        self.scale = float(scale)
        self.center = (crop_shape[1] / 2.0, crop_shape[0] / 2.0)

    @property
    def identity(self) -> bool:
        return self.angle == 0.0 and self.scale == 1.0 and self.ox == 0 and self.oy == 0

    def to_page(self, lines: list[dict]) -> list[dict]:
        if self.identity or not lines:
            return lines
        # prepared -> rotated crop -> crop -> original
        inv_rot = cv2.getRotationMatrix2D(self.center, -self.angle, 1.0)
        out = []
        for line in lines:
            x, y, w, h = (float(v) for v in line["bbox"])
            pts = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]],
                           dtype=np.float32).reshape(1, -1, 2) / self.scale
            pts = cv2.transform(pts, inv_rot).reshape(-1, 2)
            pts[:, 0] += self.ox
            pts[:, 1] += self.oy
            x0, y0 = int(round(pts[:, 0].min())), int(round(pts[:, 1].min()))
            x1, y1 = int(round(pts[:, 0].max())), int(round(pts[:, 1].max()))
            out.append({**line, "bbox": [x0, y0, max(1, x1 - x0), max(1, y1 - y0)]})
        return out


def prepare(img: np.ndarray) -> tuple[np.ndarray, Mapper]:
    """Return (prepared BGR page, mapper) for OCR.

    Never mutates the input. If nothing applies, the returned image is a
    copy and the mapper is the identity."""
    bgr = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR) if img.ndim == 2 else img.copy()
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    x0, y0, x1, y1 = _content_box(gray)
    crop = bgr[y0:y1, x0:x1]
    crop_gray = gray[y0:y1, x0:x1]

    angle = _deskew_angle(crop_gray)
    if abs(angle) >= DESKEW_MIN:
        m = cv2.getRotationMatrix2D(
            (crop.shape[1] / 2.0, crop.shape[0] / 2.0), angle, 1.0)
        rot = cv2.warpAffine(crop, m, (crop.shape[1], crop.shape[0]),
                             flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255))
    else:
        angle, rot = 0.0, crop

    flat = _flatten(cv2.cvtColor(rot, cv2.COLOR_BGR2GRAY))
    scale = _upscale_factor(flat)
    if scale > 1.0:
        flat = cv2.resize(flat, None, fx=scale, fy=scale,
                          interpolation=cv2.INTER_CUBIC)

    mapper = Mapper((x0, y0), angle, scale, crop.shape[:2])
    return cv2.cvtColor(flat, cv2.COLOR_GRAY2BGR), mapper
