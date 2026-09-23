#!/usr/bin/env python3
"""repair.py — fast CPU-only preprocessing for OCR of damaged old Tamil print
(1700s-1900s). Reuses the proven pieces of lane10_pipe (projection deskew,
gated despeckle) and lane5_run (divide-by-background); no deep-learning
denoisers, no dewarping libraries.

usage: python3 repair/repair.py IN_DIR|FILE OUT_DIR [--k K] [--threads N]

Prints the OCR input path per page: clean -> raw input; damaged -> _g.png.
The binary PNG is display-only and must never be sent to OCR.

Pipeline (in order):
  1. long side > 3000px  -> downscale to 3000px
  2. color and yellowed  -> R channel, else grayscale
  3. background correction: downscale copy to ~1100px, morph-close with an
     ellipse kernel of width/35, Gaussian smooth, upsample, divide, then
     percentile stretch
  4. denoise: fastNlMeansDenoising h=7 template=5 search=11
  5. deskew: downscale to 1200px, projection-profile search, coarse 0.5 deg
     then fine 0.05 deg; if the best score is within 2% of the 0 deg score
     the page is left unrotated
  6. width < 1200px      -> upscale 2x INTER_LANCZOS4
  7. Sauvola binarize (float32 + cv2.boxFilter, never skimage):
     k=0.15 (0.10 for very faded pages), window = odd(min(H,W)//30)
     clamped to 31-151
  8. no despeckle on upscaled or low-resolution pages

Hard constraints: CPU only, target < 2 s/page on 2 cores, and never erase
pulli dots, thin strokes, or the open loops of ி/ீ (no median/bilateral/
Gaussian denoise on low-res pages; despeckle is isolation-gated so a pulli
sitting under its consonant is never an island, and it is skipped entirely
on upscaled / low-resolution pages).

Output (damaged pages only): OUT_DIR/<name>.png (binary, display only)
                             OUT_DIR/<name>_g.png (grayscale, OCR input)
"""
import argparse
import glob
import os
import sys
import time

import cv2
import numpy as np

MAX_SIDE = 3000      # step 1
BG_SIDE = 1100       # step 3 background-estimate size
DESKEW_SIDE = 1200   # step 5 search size
UP_WIDTH = 1200      # step 6 threshold
K_NORMAL, K_FADED = 0.15, 0.10
CONTRAST_FADED = 0.25  # minimum ink-vs-paper contrast (also used by the raw-page gate)
BACKGROUND_WHITE = 240  # input paper p90 must already be near-white
NLM_H, NLM_T, NLM_S = 7, 5, 11


def T():
    return time.perf_counter()


# ---------------------------------------------------------------- step 1
def load(path):
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"cannot read {path}")
    h, w = img.shape[:2]
    if max(h, w) > MAX_SIDE:
        s = MAX_SIDE / max(h, w)
        img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    return img


# ---------------------------------------------------------------- step 2
def choose_channel(img):
    """color + yellowed -> R channel (yellowing lives in B); else gray."""
    if img.ndim == 2:
        return img, False
    small = cv2.resize(img, None, fx=0.1, fy=0.1, interpolation=cv2.INTER_AREA)
    r = small[..., 2].astype(np.int16)
    b = small[..., 0].astype(np.int16)
    bright = small.max(axis=2).astype(np.int16) > 100
    if bright.sum() < 50:                      # nothing bright: fall back
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), False
    diff = float(np.median(r[bright] - b[bright]))
    if diff > 20:                              # paper yellows = blue deficit
        return img[..., 2], True
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), False


# ---------------------------------------------------------------- step 3
def background_correct(g):
    """Divide out the paper background (lane5 bgnorm idea), then stretch."""
    h, w = g.shape
    s = min(1.0, BG_SIDE / max(h, w))
    small = (cv2.resize(g, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
             if s < 1 else g.copy())
    k = max(3, (small.shape[1] // 35) | 1)     # ellipse kernel width/35, odd
    k = min(k, max(3, (min(small.shape[:2]) - 2) | 1))
    ell = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    bg = cv2.morphologyEx(small, cv2.MORPH_CLOSE, ell)
    bg = cv2.GaussianBlur(bg, (k, k), 0)
    bg = cv2.resize(bg, (w, h), interpolation=cv2.INTER_LINEAR)
    bg = bg.astype(np.float32) + 1.0
    n = np.clip(g.astype(np.float32) / bg * 255.0, 0.0, 255.0)
    p1, p95, p99 = np.percentile(n, (1, 95, 99))
    lo, hi = p1, max(p99, p1 + 1.0)
    out = np.clip((n - lo) / (hi - lo) * 255.0, 0.0, 255.0).astype(np.uint8)
    # fadedness: INK CONTRAST (paper minus ink level), not ink amount —
    # a sparse-but-dark page is NOT faded, a washed-out page is.
    contrast = (p95 - p1) / 255.0
    return out, float(contrast)


# ---------------------------------------------------------------- step 4
def denoise(g):
    return cv2.fastNlMeansDenoising(g, None, h=NLM_H,
                                    templateWindowSize=NLM_T,
                                    searchWindowSize=NLM_S)


# ---------------------------------------------------------------- step 5
def _score(bw, a):
    """Projection-profile sharpness: higher = straighter text lines."""
    h, w = bw.shape
    M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), a, 1.0)
    r = cv2.warpAffine(bw, M, (w, h), flags=cv2.INTER_NEAREST)
    p = r.sum(axis=1, dtype=np.float64)
    return float(np.sum(np.diff(p) ** 2))


def deskew_angle(g):
    """lane10 deskew_fast2: 1200px copy, trimmed margins, coarse 0.5 deg
    then fine 0.05 deg; 2 %-of-zero-score guard keeps clean pages still."""
    s = min(1.0, DESKEW_SIDE / max(g.shape))
    sm = (cv2.resize(g, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
          if s < 1 else g)
    h, w = sm.shape
    sm = sm[h // 10:h - h // 10, w // 10:w - w // 10]   # drop page margins
    bw = (sm < 128).astype(np.uint8)
    if not bw.any():
        return 0.0

    def sc(a):
        return _score(bw, a)

    s0 = sc(0.0)
    coarse = np.arange(-5.0, 5.001, 0.5)
    a0 = float(coarse[int(np.argmax([sc(a) for a in coarse]))])
    fine = np.arange(a0 - 0.5, a0 + 0.501, 0.05)
    scores = [sc(a) for a in fine]
    best_i = int(np.argmax(scores))
    a = float(fine[best_i])
    if scores[best_i] <= 1.02 * s0:            # within 2% of straight -> don't
        a = 0.0
    return a


def rotate(g, a):
    if abs(a) < 0.05:
        return g
    h, w = g.shape
    M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), a, 1.0)
    return cv2.warpAffine(g, M, (w, h), flags=cv2.INTER_CUBIC,
                          borderValue=255)


# ---------------------------------------------------------------- step 7
def sauvola(g, k):
    """Sauvola in float32 via boxFilter. Never skimage (OOMs on big scans)."""
    win = min(g.shape) // 30
    win = max(31, min(151, win)) | 1           # odd, clamped 31-151
    f = g.astype(np.float32)
    m = cv2.boxFilter(f, cv2.CV_32F, (win, win), borderType=cv2.BORDER_REFLECT_101)
    m2 = cv2.boxFilter(f * f, cv2.CV_32F, (win, win), borderType=cv2.BORDER_REFLECT_101)
    sd = np.sqrt(np.maximum(m2 - m * m, 0.0))
    R = 128.0
    th = m * (1.0 + k * (sd / R - 1.0))
    return (np.where(f > th, 255, 0)).astype(np.uint8), win


# ---------------------------------------------------------------- step 8
def despeckle(bw, max_area=None, iso_r=None):
    """lane10's isolation-gated despeckle: only kills small components with
    NO ink nearby. A pulli sitting under its consonant is never an island,
    so dots and thin strokes survive by construction. Skipped entirely for
    upscaled / low-resolution pages (step 8)."""
    ink = (bw < 160).astype(np.uint8)
    n, lab, st, cen = cv2.connectedComponentsWithStats(ink, connectivity=8)
    H = max(bw.shape)
    max_area = max_area or max(2, int((H / 1000.0) ** 2 * 3))
    iso_r = iso_r or max(3, H // 250)
    small = np.where(st[:, cv2.CC_STAT_AREA] <= max_area)[0]
    small = small[small > 0]
    if len(small) == 0:
        return bw, 0
    keep = np.ones(n, np.uint8)
    keep[small] = 0
    keep[0] = 0
    big = keep[lab]                            # ink that is not speck
    near = cv2.dilate(big, cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (2 * iso_r + 1, 2 * iso_r + 1)))
    kill = np.zeros(n, bool)
    cx = cen[small, 0].astype(int)
    cy = cen[small, 1].astype(int)
    kill[small[near[cy, cx] == 0]] = True      # isolated only
    out = bw.copy()
    out[kill[lab]] = 255
    return out, int(kill.sum())


# ---------------------------------------------------------------- driver
def route(img):
    """Gate on the input, before background correction can whiten damaged paper."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, None, fx=0.1, fy=0.1, interpolation=cv2.INTER_AREA)
    ink, paper = np.percentile(small, (1, 95))
    contrast = (paper - ink) / 255.0
    background = float(np.percentile(small, 90))
    return contrast >= CONTRAST_FADED and background >= BACKGROUND_WHITE, contrast, background


def run(path, args, img=None):
    t = {}
    t0 = T()
    img = load(path) if img is None else img            # step 1
    t["1 downscale"] = T() - t0
    t1 = T()
    g, yellowed = choose_channel(img)                  # step 2
    orig_w = g.shape[1]
    t["2 channel"] = T() - t1
    t1 = T()
    g, contrast = background_correct(g)                  # step 3
    t["3 background"] = T() - t1
    t1 = T()
    g = denoise(g)                                     # step 4
    t["4 nlm"] = T() - t1
    t1 = T()
    ang = deskew_angle(g)                              # step 5
    g = rotate(g, ang)
    t["5 deskew"] = T() - t1
    t1 = T()
    up = False
    if g.shape[1] < UP_WIDTH:                          # step 6
        g = cv2.resize(g, None, fx=2.0, fy=2.0,
                       interpolation=cv2.INTER_LANCZOS4)
        up = True
    t["6 upscale"] = T() - t1
    t1 = T()
    k = args.k if args.k is not None else (K_FADED if contrast < CONTRAST_FADED else K_NORMAL)
    bw, win = sauvola(g, k)                            # step 7
    t["7 sauvola"] = T() - t1
    t1 = T()
    low_res = orig_w < UP_WIDTH
    killed = -1
    if not (up or low_res):                            # step 8
        bw, killed = despeckle(bw)
    t["8 despeckle"] = T() - t1
    total = T() - t0
    info = dict(yellowed=yellowed, gap=round(contrast, 3), k=k, win=win,
                angle=ang, up=up, low_res=low_res, killed=killed,
                shape=bw.shape, t=t, total=total)
    return g, bw, info


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("inp")
    ap.add_argument("out")
    ap.add_argument("--k", type=float, default=None,
                    help="override Sauvola k (default: 0.15, 0.10 if faded)")
    ap.add_argument("--save-gray", action="store_true",
                    help="deprecated: damaged pages always save grayscale for OCR")
    ap.add_argument("--threads", type=int, default=2,
                    help="OpenCV threads (default 2 = budget cores)")
    args = ap.parse_args()

    cv2.setNumThreads(args.threads)                    # 2-core budget
    os.makedirs(args.out, exist_ok=True)

    if os.path.isdir(args.inp):
        files = sorted(p for p in glob.glob(os.path.join(args.inp, "*"))
                       if p.lower().endswith((".png", ".jpg", ".jpeg",
                                              ".tif", ".tiff", ".bmp")))
    else:
        files = [args.inp]
    if not files:
        sys.exit(f"no images in {args.inp}")

    for p in files:
        name = os.path.splitext(os.path.basename(p))[0]
        try:
            img = load(p)
            clean, contrast, background = route(img)
            ocr_input = p if clean else os.path.join(args.out, name + "_g.png")
            print(f"{name}: {'CLEAN' if clean else 'DAMAGED'} "
                  f"ink_contrast={contrast:.3f} (min={CONTRAST_FADED}) "
                  f"background_p90={background:.1f} (min={BACKGROUND_WHITE}) "
                  f"OCR={ocr_input}", flush=True)
            if clean:
                continue
            g, bw, info = run(p, args, img)
            if not cv2.imwrite(ocr_input, g):
                raise OSError(f"cannot write OCR input {ocr_input}")
            out = os.path.join(args.out, name + ".png")
            if not cv2.imwrite(out, bw):
                raise OSError(f"cannot write display image {out}")
        except Exception as e:
            print(f"{name}: FAILED ({e})", flush=True)
            continue
        steps = " ".join(f"{k.split()[0]}={v*1000:.0f}ms"
                         for k, v in info["t"].items())
        print(f"{name}: {info['shape'][1]}x{info['shape'][0]} "
              f"yellowed={info['yellowed']} gap={info['gap']} k={info['k']} "
              f"win={info['win']} skew={info['angle']:.2f} "
              f"up={info['up']} despeckle_killed={info['killed']} "
              f"total={info['total']:.2f}s  [{steps}]", flush=True)


if __name__ == "__main__":
    main()
