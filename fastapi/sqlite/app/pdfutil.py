"""Page loading for Pink Cloud.

Turns any accepted upload (PDF/JPG/PNG/TIFF) into BGR images, one per
page, with the long side capped at 1600 px so all downstream metrics and
bboxes live in one consistent coordinate space.

PDF   -> pypdfium2 renders each page at ~200 DPI (bitmap.to_pil() needs
         Pillow, which is pinned in requirements.txt).
Images-> cv2. Multi-page TIFFs are read with cv2.imreadmulti so ALL
         pages are kept (cv2.imdecode/imread would return only the first).
"""

import threading
from pathlib import Path

import cv2
import numpy as np

MAX_SIDE = 1600  # cap the long side; smaller pages stay as-is

# PDFium is NOT thread-safe, and FastAPI runs sync routes in a thread pool.
# Two requests touching pypdfium2 at once (uploads, page renders, PDF export)
# corrupt the native heap and kill the whole server process ("corrupted
# double-linked list", exit 134). Every pypdfium2 call in the app must hold
# this lock. RLock so export.build_pdf can hold it while calling load_pages.
PDFIUM_LOCK = threading.RLock()


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


def probe_decode(ext: str, data: bytes) -> None:
    """Cheap decodability check on raw upload bytes — raises ValueError
    if the file cannot be decoded. Used by main.py to reject corrupt
    uploads with 4xx BEFORE a job row is created.
    """
    if ext == ".pdf":
        import pypdfium2 as pdfium  # lazy import

        with PDFIUM_LOCK:
            pdf = pdfium.PdfDocument(data)  # accepts bytes; raises on corrupt
            page_count = len(pdf)
            pdf.close()
        if page_count == 0:
            raise ValueError("PDF has no pages")
        return

    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("not a decodable image")


def load_pages(path: Path) -> list[np.ndarray]:
    """Return a list of page images (BGR) for the given master file."""
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        import pypdfium2 as pdfium  # imported lazily; cheap and CPU-only

        with PDFIUM_LOCK:
            pdf = pdfium.PdfDocument(str(path))
            pages: list[np.ndarray] = []
            # scale = zoom factor; 200/72 makes ~200 DPI renders.
            for i in range(len(pdf)):
                bitmap = pdf[i].render(scale=200 / 72)
                # .to_pil() keeps us decoupled from pypdfium2's raw buffer API
                # (requires Pillow — pinned in requirements.txt), and numpy can
                # consume a PIL image directly.
                pil_img = bitmap.to_pil().convert("RGB")
                img = np.array(pil_img)[:, :, ::-1]  # RGB -> BGR for cv2
                pages.append(_cap_1600(np.ascontiguousarray(img)))
            pdf.close()
        return pages

    if suffix in (".tif", ".tiff"):
        # Multi-page TIFF: imreadmulti returns EVERY page; imdecode would
        # silently give us only the first one.
        ok, pages = cv2.imreadmulti(str(path), flags=cv2.IMREAD_COLOR)
        if not ok or not pages:
            raise ValueError(f"could not decode TIFF: {path.name}")
        return [_cap_1600(p) for p in pages]

    # Single-page image (jpg/png): decode from the bytes we already have.
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
