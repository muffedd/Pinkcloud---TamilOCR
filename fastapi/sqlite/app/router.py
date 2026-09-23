"""Quality router for Pink Cloud.

Scores every page with 4 cheap CV metrics and assigns a badge:

  FAST  -> clean page, go straight to OCR.
  HEAVY -> damaged page, would go to a repair pass before OCR.

The metrics use cv2 + numpy only, no OCR model, so routing is instant
and works fully offline. Thresholds are deliberately simple and in ONE
place so teammates can tune them during the hackathon.
"""

from dataclasses import dataclass, asdict

import cv2
import numpy as np


@dataclass
class PageScores:
    """The 4 metrics computed on one page image (grayscale)."""

    blur: float        # Laplacian variance: higher = sharper
    contrast: float    # ink/background ratio: higher = clearer text
    noise: float       # high-frequency energy estimate: lower = cleaner
    skew_deg: float    # signed text tilt in degrees; + = counter-clockwise


def compute_scores(gray: np.ndarray) -> PageScores:
    """Compute all 4 metrics on a grayscale uint8 image."""
    if gray.ndim != 2:
        raise ValueError("compute_scores expects a grayscale image")

    # ---- 1) Blur: variance of the Laplacian on a denoised copy -----------
    # A sharp text page has lots of strong edges -> big Laplacian variance.
    # A blurry/scanned-out-of-focus page -> small variance.
    # Measured AFTER a 5x5 median filter so sensor grain / JPEG noise does not
    # inflate "sharpness" (on the raw image a noisy, blurred scan scored
    # sharper than a clean page and routed FAST).
    denoised = cv2.medianBlur(gray, 5)
    blur = float(cv2.Laplacian(denoised, cv2.CV_64F).var())

    # ---- 2) Contrast: ink vs background brightness ratio ------------------
    # Otsu splits pixels into dark (ink) and bright (paper) groups.
    # Contrast = mean(paper) - mean(ink), normalised by mean(paper).
    # A page with no ink at all (blank / uniform) reports 0.0, not 1.0.
    _thresh, ink_bg = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    ink_vals = gray[ink_bg == 0].astype(np.float32)
    bg_vals = gray[ink_bg == 255].astype(np.float32)
    if ink_vals.size == 0 or bg_vals.size == 0 or float(gray.std()) < 1.0:
        contrast = 0.0
    else:
        bg_mean = float(bg_vals.mean())
        contrast = (bg_mean - float(ink_vals.mean())) / bg_mean if bg_mean > 0 else 0.0

    # ---- 3) Noise: high-frequency energy estimate -------------------------
    # Median filter removes fine grain; (original - median) keeps only the
    # grain. Its std is a simple, robust noise estimate.
    med = cv2.medianBlur(gray, 3)
    noise = float((gray.astype(np.float32) - med.astype(np.float32)).std())

    # ---- 4) Skew: signed angle of the text lines --------------------------
    skew_deg = estimate_skew(gray)

    return PageScores(blur=blur, contrast=float(contrast), noise=noise, skew_deg=skew_deg)


def estimate_skew(gray: np.ndarray) -> float:
    """Signed skew of the text lines in degrees, in [-45, +45].

    Positive = text rotated counter-clockwise (lines rise to the right);
    rotating the page by -skew_deg with cv2.getRotationMatrix2D deskews it.

    Projection-profile search: rotate a small binarised copy through
    candidate angles and keep the one where the row-ink profile is sharpest
    (text lines line up with pixel rows). This replaced a minAreaRect
    heuristic that mis-read horizontal lines (OpenCV >= 4.5 returns
    (short, long, -90deg) for them): clean pages read 45deg and routed HEAVY,
    while an 8deg rotated photo read 0.0. Returns 0.0 on blank pages.
    """
    h, w = gray.shape
    scale = 600.0 / max(h, w)
    if scale < 1.0:
        small = cv2.resize(gray, (max(1, int(w * scale)), max(1, int(h * scale))),
                           interpolation=cv2.INTER_AREA)
    else:
        small = gray
    small = cv2.medianBlur(small, 3)
    if float(small.std()) < 2.0:          # uniform page: nothing to measure
        return 0.0
    binary = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    ink = float((binary > 0).mean())
    if ink < 0.002 or ink > 0.5:          # no text, or Otsu picked paper as ink
        return 0.0

    sh, sw = binary.shape
    center = (sw / 2.0, sh / 2.0)

    def sharpness(angle: float) -> float:
        m = cv2.getRotationMatrix2D(center, float(angle), 1.0)
        rot = cv2.warpAffine(binary, m, (sw, sh), flags=cv2.INTER_NEAREST, borderValue=0)
        profile = rot.sum(axis=1, dtype=np.float64)
        return float(np.sum(np.diff(profile) ** 2))

    best = max(np.arange(-45.0, 45.01, 1.0), key=sharpness)          # coarse
    best = max(np.arange(best - 1.0, best + 1.001, 0.1), key=sharpness)  # fine
    return round(float(-best), 1) + 0.0


# ----------------------------------------------------------------------------
# Threshold rule — the single place to tune. Defaults are hackathon-pragmatic:
#
#   FAST  requires ALL of:  blur >= 120   (sharp enough; median-filtered)
#                           contrast >= 0.25 (ink clearly darker than paper)
#                           noise <= 12     (not grainy)
#                           |skew_deg| <= 5 (roughly straight)
#   HEAVY otherwise. Tune these live by looking at /jobs results.
# ----------------------------------------------------------------------------
THRESHOLDS = {"blur": 120.0, "contrast": 0.25, "noise": 12.0, "skew_deg": 5.0}


def choose_profile(scores: PageScores) -> tuple[str, PageScores]:
    """Return (badge, scores). FAST if every metric is within bounds."""
    s = asdict(scores)
    is_fast = (
        s["blur"] >= THRESHOLDS["blur"]
        and s["contrast"] >= THRESHOLDS["contrast"]
        and s["noise"] <= THRESHOLDS["noise"]
        and abs(s["skew_deg"]) <= THRESHOLDS["skew_deg"]
    )
    profile = "FAST" if is_fast else "HEAVY"
    return profile, scores