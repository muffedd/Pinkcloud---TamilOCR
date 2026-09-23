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
    # Works on inverted scans too because Otsu just picks a split point.
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
    # Find the horizontal text lines via morphological closing, then take
    # the min-area rotated rectangle around the biggest line box.
    # Fallback: 0.0 if we can't find any structure (blank page etc.).
    skew_deg = 0.0
    binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    # Join characters into solid text-line blobs.
    line_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (40, 1))
    lines_img = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, line_kernel)
    contours, _ = cv2.findContours(lines_img, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if contours:
        # Prefer the widest big blob (a text line, w > h); fall back to the
        # largest blob overall (handles near-square blocks).
        def rect_of(c):
            (_cx, _cy), (rw, rh), ang = cv2.minAreaRect(c)
            return cv2.contourArea(c), rw, rh, ang

        infos = [rect_of(c) for c in contours]
        wide = [i for i in infos if i[1] >= i[2]]
        _area, rw, rh, angle = max(wide or infos, key=lambda i: i[0])
        # cv2 angle convention: make the LONG side the "width", then fold
        # the angle into [-45, +45). Without this, straight pages can read
        # as 90 degrees.
        if rw < rh:
            angle += 90
        angle %= 90
        if angle > 45:
            angle -= 90
        skew_deg = float(abs(angle))

    return PageScores(blur=blur, contrast=float(contrast), noise=noise, skew_deg=skew_deg)


# ----------------------------------------------------------------------------
# Threshold rule — the single place to tune. Defaults are hackathon-pragmatic:
#
#   FAST  requires ALL of:  blur >= 100   (sharp enough)
#                           contrast >= 0.25 (ink clearly darker than paper)
#                           noise <= 12     (not grainy)
#                           skew_deg <= 5   (roughly straight)
#   HEAVY otherwise. Tune these live by looking at /jobs results.
# ----------------------------------------------------------------------------
THRESHOLDS = {"blur": 100.0, "contrast": 0.25, "noise": 12.0, "skew_deg": 5.0}


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