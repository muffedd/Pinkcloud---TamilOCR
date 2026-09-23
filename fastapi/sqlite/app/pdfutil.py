"""Page loading for Pink Cloud.

Turns any accepted upload (PDF/JPG/PNG/TIFF) into grayscale cv2 images,
one per page, with the long side capped at 1600 px so all downstream
metrics and bboxes live in one consistent coordinate space.
"""

from pathlib import Path

import cv2
import numpy as np

MAX_SIDE = 1600  # cap the long side; smaller pages stay as-is


def _cap_1600(img: np.ndarray) -> np.ndarray:
    """Scale an image down so its longest side is at most MAX_SIDE px."""
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest <= MAX_SIDE:
        return img
    scale = MAX_SIDE / longest
    new_w, new_h = int(round(w * scale)), int(round(h * scale))
    # INTER_AREA is the right filter for downscaling.
    return cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)


def load_pages(path: Path) -> list[np.ndarray]:
    """Return a list of page images (BGR) for the given master file.

    PDF  -> pypdfium2 renders each page at ~200 DPI, then we cap at 1600.
    Image -> cv2.imread directly (handles jpg/png/tiff on Windows).
    """
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        import pypdfium2 as pdfium  # imported lazily; cheap and CPU-only

        pdf = pdfium.PdfDocument(str(path))
        pages: list[np.ndarray] = []
        # scale = zoom factor; 200/72 makes ~200 DPI renders.
        for i in range(len(pdf)):
            bitmap = pdf[i].render(scale=200 / 72)
            # .to_pil() keeps us decoupled from pypdfium2's raw buffer API,
            # and numpy can consume a PIL image directly.
            pil_img = bitmap.to_pil().convert("RGB")
            img = np.array(pil_img)[:, :, ::-1]  # RGB -> BGR for cv2
            pages.append(_cap_1600(np.ascontiguousarray(img)))
        pdf.close()
        return pages

    # Plain image: cv2.imread handles jpg/png/tiff via OpenCV's own codecs
    # (no poppler, no system dependencies — good for Windows laptops).
    # cv2.imdecode lets us return a clean None on failure instead of crashing.
    data = path.read_bytes()
    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"could not decode image: {path.name}")
    return [_cap_1600(img)]


def to_gray(img: np.ndarray) -> np.ndarray:
    """BGR/RGB -> grayscale, for the router metrics."""
    if img.ndim == 2:
        return img
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)