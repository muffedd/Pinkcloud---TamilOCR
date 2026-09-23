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
    skew_deg: float    # how tilted the text block is (degrees)


def compute_scores(gray: np.ndarray) -> PageScores:
    """Compute all 4 metrics on a grayscale uint8 image."""
    if gray.ndim != 2:
        raise ValueError("compute_scores expects a grayscale image")

    # ---- 1) Blur: variance of the Laplacian -------------------------------
    # A sharp text page has lots of strong edges -> big Laplacian variance.
    # A blurry/scanned-out-of-focus page -> small variance.
    blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())

    # ---- 2) Contrast: ink vs background brightness ratio ------------------
    # Otsu splits pixels into dark (ink) and bright (paper) groups.
    # Contrast = mean(paper) - mean(ink), normalised by mean(paper).
    _mask, ink_bg = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    ink_vals = gray[ink_bg == 0].astype(np.float32)
    bg_vals = gray[ink_bg == 255].astype(np.float32)
    ink_mean = float(ink_vals.mean()) if ink_vals.size else 0.0
    bg_mean = float(bg_vals.mean()) if bg_vals.size else 0.0
    contrast = (bg_mean - ink_mean) / bg_mean if bg_mean > 0 else 0.0

    # ---- 3) Noise: high-frequency energy estimate -------------------------
    # Median filter removes fine grain; (original - median) keeps only the
    # grain. Its std is a simple, robust noise estimate.
    med = cv2.medianBlur(gray, 3)
    noise = float((gray.astype(np.float32) - med.astype(np.float32)).std())

    # ---- 4) Skew: angle of the dominant text line -------------------------
    # cv2.minAreaRect returns (center, (w, h), angle) where it may hand us
    # the rectangle with the SHORT side as "width" (angle near +/-90 for
    # perfectly horizontal text). The old rw >= rh filter dropped exactly
    # those blobs, so clean pages read 0.0 and rotated photos misread.
    # Fix: normalize every rect to "long side = width" first, THEN fold
    # the angle into [-45, +45).
    skew_deg = 0.0
    binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    # Join characters into solid text-line blobs.
    line_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (40, 1))
    lines_img = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, line_kernel)
    contours, _ = cv2.findContours(lines_img, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    best_area, best_angle = 0.0, 0.0
    for c in contours:
        area = cv2.contourArea(c)
        (_cx, _cy), (rw, rh), angle = cv2.minAreaRect(c)
        if rw < rh:
            # cv2 reported the short side as width: rotate the frame so the
            # long side is the width, and compensate the angle by 90 deg.
            angle += 90
        angle %= 90.0  # Python modulo -> always in [0, 90)
        if angle > 45:
            angle -= 90
        if area > best_area:
            best_area, best_angle = area, abs(angle)
    skew_deg = float(best_angle)

    return PageScores(blur=blur, contrast=float(contrast), noise=noise, skew_deg=skew_deg)


# ----------------------------------------------------------------------------
# Threshold rule — the single place to tune. Tuned against CICT-style scans
# (clean Kural prints + damaged/stained/rotated samples) with
# tools/tune_thresholds.py; see RUN.md. Defaults:
#
#   FAST  requires ALL of:  blur >= 80    (sharp enough)
#                           contrast >= 0.20 (ink clearly darker than paper)
#                           noise <= 15    (not grainy; typical scans ~5-12)
#                           skew_deg <= 7  (roughly straight)
#   HEAVY otherwise.
#
# blur 100->80 and noise 12->15: real 200-DPI scans of clean paper sit at
# blur ~90-400 with grain noise ~10-14, which the old values misclassified
# as HEAVY. Tune with: python tools/tune_thresholds.py <folder-of-samples>
# ----------------------------------------------------------------------------
THRESHOLDS = {"blur": 80.0, "contrast": 0.20, "noise": 15.0, "skew_deg": 7.0}


def choose_profile(scores: PageScores) -> tuple[str, PageScores]:
    """Return (badge, scores). FAST if every metric is within bounds."""
    s = asdict(scores)
    is_fast = (
        s["blur"] >= THRESHOLDS["blur"]
        and s["contrast"] >= THRESHOLDS["contrast"]
        and s["noise"] <= THRESHOLDS["noise"]
        and s["skew_deg"] <= THRESHOLDS["skew_deg"]
    )
    profile = "FAST" if is_fast else "HEAVY"
    return profile, scores