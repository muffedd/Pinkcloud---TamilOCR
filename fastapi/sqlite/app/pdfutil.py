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
    Image -> OpenCV decode (jpg/png; every page of a multi-page TIFF).
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

    # Plain image: decode with OpenCV's own codecs (no poppler, no system
    # dependencies - good for Windows laptops). imdecode returns None on
    # failure instead of crashing.
    return [_cap_1600(img) for img in _decode_image_bytes(path.read_bytes(), suffix, path.name)]


def _decode_image_bytes(data: bytes, suffix: str, name: str = "upload") -> list[np.ndarray]:
    """Decode image bytes into one BGR image per page.

    TIFF can hold several pages; cv2.imdecode only returns the first one,
    so TIFFs go through cv2.imdecodemulti (Pillow as a fallback) to keep
    every page. JPG/PNG are always a single page.
    """
    buf = np.frombuffer(data, dtype=np.uint8)
    if suffix in (".tif", ".tiff"):
        pages = _decode_tiff_pages(data, buf)
        if not pages:
            raise ValueError(f"could not decode image: {name}")
        return pages

    img = cv2.imdecode(buf, cv2.IMREAD_COLOR) if buf.size else None
    if img is None:
        raise ValueError(f"could not decode image: {name}")
    return [img]


def _decode_tiff_pages(data: bytes, buf: np.ndarray) -> list[np.ndarray]:
    """All pages of a (possibly multi-page) TIFF as BGR images."""
    if buf.size == 0:
        return []
    try:
        ok, mats = cv2.imdecodemulti(buf, cv2.IMREAD_COLOR)
    except cv2.error:
        ok, mats = False, None
    if ok and mats:
        return [np.ascontiguousarray(m) for m in mats]

    # Fallback for TIFF flavours OpenCV can't read: Pillow walks every frame.
    try:
        import io

        from PIL import Image, ImageSequence

        pages = []
        with Image.open(io.BytesIO(data)) as im:
            for frame in ImageSequence.Iterator(im):
                rgb = np.array(frame.convert("RGB"))
                pages.append(np.ascontiguousarray(rgb[:, :, ::-1]))  # RGB -> BGR
        return pages
    except Exception:
        return []


# First bytes of each accepted format. Checked before a job is created.
_MAGIC = {
    ".pdf": (b"%PDF-",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".tif": (b"II*\x00", b"MM\x00*"),
    ".tiff": (b"II*\x00", b"MM\x00*"),
}


def validate_upload(data: bytes, suffix: str) -> None:
    """Raise ValueError unless the bytes are a readable file of this type.

    Runs before a job is created so bad uploads get a clean HTTP 400:
    the magic bytes must match the extension, and the file must open
    (PDF has at least one page, images decode).
    """
    suffix = suffix.lower()
    magic = _MAGIC.get(suffix)
    if magic is None:
        raise ValueError(f"unsupported extension: {suffix or '(none)'}")
    if not data:
        raise ValueError("empty file")
    if not data.startswith(magic):
        raise ValueError(f"file contents do not match the {suffix} extension")

    if suffix == ".pdf":
        import pypdfium2 as pdfium

        try:
            pdf = pdfium.PdfDocument(data)
        except Exception:
            raise ValueError("could not open PDF (damaged or password-protected)") from None
        try:
            if len(pdf) < 1:
                raise ValueError("PDF has no pages")
        finally:
            pdf.close()
        return

    _decode_image_bytes(data, suffix)  # raises ValueError if undecodable


def to_gray(img: np.ndarray) -> np.ndarray:
    """BGR/RGB -> grayscale, for the router metrics."""
    if img.ndim == 2:
        return img
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
