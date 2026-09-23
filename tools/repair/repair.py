#!/usr/bin/env python3
"""Heavy pre-OCR repair for old printed Tamil pages. CPU only (OpenCV + NumPy).
Writes image files only. Does not touch router.py / ocr.py / schema.json.

usage: python3 repair.py IN_DIR OUT_DIR [--binary] [--no-stamp] [--no-crop] [--debug]
out: OUT_DIR/<name>.png (grayscale, primary)  + <name>_bin.png if --binary
"""
import argparse, glob, os, sys
import cv2
import numpy as np

MAX_SIDE = 3500          # never send bigger than this (long side, px)
TARGET_CHAR_H = 32       # aim for ~32 px median glyph height
MIN_CHAR_H = 20          # upscale only below this

def load(p):
    img = cv2.imread(p, cv2.IMREAD_COLOR)   # applies EXIF rotation
    if img is None:
        raise ValueError(f"cannot read {p}")
    h, w = img.shape[:2]
    s = MAX_SIDE / max(h, w)
    if s < 1:
        img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    return img

def order_pts(p):
    p = p.reshape(4, 2).astype(np.float32)
    s, d = p.sum(1), np.diff(p, axis=1).ravel()
    return np.array([p[s.argmin()], p[d.argmin()], p[s.argmax()], p[d.argmax()]], np.float32)

def crop_page(img):
    """Find the paper against the background; flatten if 4 corners, else bbox.
    Anything outside the paper (tears, table, fingers) is painted white."""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    g = cv2.GaussianBlur(g, (9, 9), 0)
    _, m = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((25, 25), np.uint8))
    cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cs:
        return img
    c = max(cs, key=cv2.contourArea)
    H, W = g.shape
    if cv2.contourArea(c) < 0.25 * H * W:      # page not found / already a flat scan
        return img
    out = img.copy()
    mask = np.zeros((H, W), np.uint8)
    cv2.drawContours(mask, [c], -1, 255, -1)
    out[mask == 0] = 255                        # torn/ragged margins -> white
    approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
    if len(approx) == 4:
        src = order_pts(approx)
        wA = np.linalg.norm(src[1] - src[0]); wB = np.linalg.norm(src[2] - src[3])
        hA = np.linalg.norm(src[3] - src[0]); hB = np.linalg.norm(src[2] - src[1])
        Wn, Hn = int(max(wA, wB)), int(max(hA, hB))
        dst = np.array([[0, 0], [Wn - 1, 0], [Wn - 1, Hn - 1], [0, Hn - 1]], np.float32)
        M = cv2.getPerspectiveTransform(src, dst)
        return cv2.warpPerspective(out, M, (Wn, Hn), flags=cv2.INTER_CUBIC,
                                   borderValue=(255, 255, 255))
    x, y, w, h = cv2.boundingRect(c)
    return out[y:y + h, x:x + w]

def remove_color_ink(img):
    """Blank strongly coloured marks (blue/violet/red/green stamps, coloured pen).
    Black/brown print and yellow paper are kept (low sat or yellow hue)."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    yellowish = (h >= 8) & (h <= 35)            # aged paper / foxing: keep
    colored = (s > 70) & (v > 40) & ~yellowish
    m = colored.astype(np.uint8) * 255
    # no dilation: print pixels under the stamp are dark/low-sat and must survive
    if m.mean() / 255 < 0.0005:
        return img, 0.0
    # fill with local paper colour, not pure white (avoids hard halos)
    bg = cv2.medianBlur(img, 31)
    out = img.copy()
    out[m > 0] = bg[m > 0]
    return out, float(m.mean() / 255)

def flatten_background(gray):
    """Divide by estimated paper background: kills yellowing, stains, shadows."""
    H, W = gray.shape
    f = 0.25
    sm = cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    k = max(9, (min(sm.shape) // 25) | 1)
    bg = cv2.morphologyEx(sm, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    bg = cv2.medianBlur(bg, k if k <= 255 else 255)
    bg = cv2.resize(bg, (W, H), interpolation=cv2.INTER_CUBIC)
    bg = cv2.GaussianBlur(bg, (0, 0), 5)
    norm = cv2.divide(gray, np.maximum(bg, 1), scale=255)
    lo, hi = np.percentile(norm, (1, 90))       # paper is the majority -> white
    norm = np.clip((norm.astype(np.float32) - lo) * 255.0 / max(hi - lo, 1), 0, 255)
    return norm.astype(np.uint8)

def sauvola(gray, win=31, k=0.2, R=128):
    g = gray.astype(np.float64)
    mean = cv2.boxFilter(g, -1, (win, win))
    sq = cv2.boxFilter(g * g, -1, (win, win))
    std = np.sqrt(np.maximum(sq - mean * mean, 0))
    t = mean * (1 + k * (std / R - 1))
    return np.where(g > t, 255, 0).astype(np.uint8)

def char_height(binary):
    inv = 255 - binary
    n, _, st, _ = cv2.connectedComponentsWithStats(inv, 8)
    hs = st[1:, cv2.CC_STAT_HEIGHT]
    a = st[1:, cv2.CC_STAT_AREA]
    hs = hs[(a > 15) & (hs > 4) & (hs < binary.shape[0] / 8)]
    return float(np.median(hs)) if len(hs) > 20 else 0.0

def despeckle(gray, binary, ch):
    """Remove only ISOLATED specks. A tiny dot near real glyphs (Tamil pulli,
    vowel-sign fragments) is kept; a tiny dot in open paper is erased."""
    inv = 255 - binary
    n, lab, st, _ = cv2.connectedComponentsWithStats(inv, 8)
    if n < 2 or ch <= 0:
        return gray, 0
    area = st[:, cv2.CC_STAT_AREA]
    tiny_max = max(4, int(0.012 * ch * ch))         # pulli is ~0.015-0.03 ch^2
    big = np.isin(lab, np.where(area > tiny_max)[0][1:])
    near = cv2.dilate(big.astype(np.uint8), np.ones((int(0.6 * ch) | 1,) * 2, np.uint8)) > 0
    tiny_ids = np.where((area <= tiny_max) & (np.arange(n) > 0))[0]
    kill = np.isin(lab, tiny_ids) & ~near
    out = gray.copy()
    out[kill] = 255
    return out, int(kill.sum())

def deskew_angle(binary, rng=5.0, step=0.1):
    inv = (255 - binary)
    s = 1000 / max(inv.shape)
    small = cv2.resize(inv, None, fx=min(s, 1), fy=min(s, 1), interpolation=cv2.INTER_AREA)
    h, w = small.shape
    best, best_a = -1, 0.0
    for a in np.arange(-rng, rng + 1e-9, step):
        M = cv2.getRotationMatrix2D((w / 2, h / 2), a, 1)
        r = cv2.warpAffine(small, M, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
        score = np.var(r.sum(1, dtype=np.float64))
        if score > best:
            best, best_a = score, a
    return float(best_a)

def rotate(gray, a):
    if abs(a) < 0.15:
        return gray
    h, w = gray.shape
    M = cv2.getRotationMatrix2D((w / 2, h / 2), a, 1)
    cos, sin = abs(M[0, 0]), abs(M[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    M[0, 2] += nw / 2 - w / 2; M[1, 2] += nh / 2 - h / 2
    return cv2.warpAffine(gray, M, (nw, nh), flags=cv2.INTER_CUBIC, borderValue=255)

def repair(path, args):
    img = load(path)
    info = {}
    if not args.no_crop:
        img = crop_page(img)
    if not args.no_stamp:
        img, info["color_ink_frac"] = remove_color_ink(img)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.fastNlMeansDenoising(gray, None, h=args.nlm, templateWindowSize=7, searchWindowSize=21)
    gray = flatten_background(gray)
    b = sauvola(gray)
    a = deskew_angle(b); info["skew_deg"] = round(a, 2)
    gray = rotate(gray, a)
    b = sauvola(gray)
    ch = char_height(b); info["char_h_px"] = round(ch, 1)
    if 0 < ch < MIN_CHAR_H:
        f = min(TARGET_CHAR_H / ch, 3.0, MAX_SIDE / max(gray.shape))
        if f > 1.05:
            gray = cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
            info["upscale"] = round(f, 2)
            b = sauvola(gray); ch = char_height(b)
    gray, info["specks_px"] = despeckle(gray, b, ch)
    return gray, info

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inp"); ap.add_argument("out")
    ap.add_argument("--binary", action="store_true", help="also write _bin.png (A/B only)")
    ap.add_argument("--no-stamp", action="store_true")
    ap.add_argument("--no-crop", action="store_true")
    ap.add_argument("--nlm", type=int, default=7, help="denoise strength 5-12")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    paths = [args.inp] if os.path.isfile(args.inp) else sorted(
        p for e in ("jpg", "jpeg", "png", "tif", "tiff", "bmp", "webp", "JPG", "PNG")
        for p in glob.glob(os.path.join(args.inp, f"*.{e}")))
    for p in paths:
        name = os.path.splitext(os.path.basename(p))[0]
        try:
            g, info = repair(p, args)
        except Exception as e:
            print(f"FAIL {name}: {e}", file=sys.stderr); continue
        cv2.imwrite(os.path.join(args.out, name + ".png"), g)
        if args.binary:
            cv2.imwrite(os.path.join(args.out, name + "_bin.png"), sauvola(g))
        print(name, info)

if __name__ == "__main__":
    main()
