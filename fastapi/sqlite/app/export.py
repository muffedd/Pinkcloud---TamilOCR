"""Export + processing receipt for Pink Cloud (slice: pipe-export-receipt).

Three outputs for a finished job:
  - build_receipt()  -> dict: page count, auto vs human-review counts,
                        corrections, reviewer, times, master SHA-256
                        (re-verified against the stored master on disk).
  - build_txt()      -> str: provenance header + page text in reading order.
  - build_docx()     -> bytes: Word document, same text as build_txt()
                        (one heading per page) + the receipt as a final
                        section. Needs python-docx.
  - build_pdf()      -> bytes: searchable PDF. Each page is the scan image
                        with an INVISIBLE text layer (one text object per OCR
                        line, placed and stretched over the line bbox, the
                        hOCR-to-PDF idea), plus a visible receipt page at the
                        end and the provenance in the PDF Info dictionary.

Offline, pinned deps only: pypdfium2 (PDFium page-object API) + Pillow.
The Tamil text layer uses the bundled fonts/noto-sans-tamil.ttf, embedded
as a CID font so PDFium writes a ToUnicode map -> Ctrl+F and pdftotext
return the real Unicode Tamil text. The glyphs are not shaped (no
HarfBuzz in PDFium's writer), which does not matter: the layer is
invisible and only exists for search / copy.

Bboxes in the page JSON are on the 1600px-capped image from
pdfutil.load_pages(), so we embed exactly that image and map
1 px -> PT_PER_PX points (the capped image is treated as 150 dpi).
"""

from __future__ import annotations

import ctypes
import io
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import pdfutil as _pdfutil
from . import storage
from .schema_out import CONFIDENCE_REVIEW_FLOOR, STUB_MARK, line_needs_review

PT_PER_PX = 72.0 / 150.0
FONT_PATH = Path(__file__).resolve().parents[3] / "fonts" / "noto-sans-tamil.ttf"
RECEIPT_VERSION = 1


# --------------------------------------------------------------------------
# saved reviewer corrections (uploads/<job_id>/corrections.json)
# --------------------------------------------------------------------------

CORRECTIONS_FILE = "corrections.json"


def load_saved_corrections(job_id: str) -> list[dict]:
    """Read the job's saved correction map written by the corrections route.

    Shape: {"corrections": [{page, line, word, before, after}, ...], ...}.
    Absent, unreadable or malformed files give [] (export falls back to raw
    OCR text); malformed entries are skipped individually."""
    path = storage.UPLOAD_ROOT / job_id / CORRECTIONS_FILE
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # missing, unreadable, not JSON
        return []
    items = doc.get("corrections") if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        return []
    out = []
    for c in items:
        try:
            page, word = int(c["page"]), int(c["word"])
            line, after = str(c["line"]), str(c["after"])
        except Exception:
            continue
        before = c.get("before")
        if page < 1 or word < 1 or not after.strip():
            continue
        out.append({"page": page, "line": line, "word": word,
                    "before": None if before is None else str(before),
                    "after": after.strip()})
    return out


def apply_corrections(pages: list[dict], corrections: list[dict]) -> list[dict]:
    """Return a copy of pages with each correction's `after` substituted for
    the word at (page, line id, 1-based word index; line body split on
    whitespace). A correction whose `before` no longer matches the OCR word
    (stale map) or points outside the line is skipped, never guessed.
    Applied ones are appended to the page copy's `corrections` list with
    tier "human", so the receipt counts them. Stored job JSON is untouched."""
    if not corrections:
        return pages
    by_key: dict[tuple, dict] = {}
    for c in corrections:
        by_key[(c["page"], c["line"], c["word"])] = c  # last one wins
    out = []
    for i, p in enumerate(pages, start=1):
        pno = int(p.get("page", i))
        new_lines, applied = [], []
        for line in p.get("lines") or []:
            words = str(line.get("body", "")).split()
            changed = False
            for w in range(1, len(words) + 1):
                c = by_key.get((pno, str(line.get("id")), w))
                if c is None:
                    continue
                if c["before"] is not None and c["before"] != words[w - 1]:
                    continue
                words[w - 1] = c["after"]
                changed = True
                applied.append({"tier": "human", "line": line.get("id"),
                                "word": w, "before": c["before"],
                                "after": c["after"]})
            new_lines.append({**line, "body": " ".join(words)} if changed else line)
        if applied:
            q = dict(p)
            q["lines"] = new_lines
            q["text"] = "\n".join(
                l.get("body", "") for l in sorted(new_lines, key=lambda l: l.get("seq", 0)))
            q["corrections"] = list(p.get("corrections") or []) + applied
            out.append(q)
        else:
            out.append(p)
    return out


def apply_saved_corrections(job_id: str, pages: list[dict]) -> list[dict]:
    """pages with the job's saved corrections applied (raw pages on any error)."""
    try:
        return apply_corrections(pages, load_saved_corrections(job_id))
    except Exception:
        return pages


# --------------------------------------------------------------------------
# receipt
# --------------------------------------------------------------------------

def _line_needs_human(line: dict, page: dict) -> bool:
    """A line goes to a human if its confidence is under the review floor,
    it is stub output, or its page was routed HEAVY. The rule itself lives
    in schema_out.line_needs_review (it is also the per-line needs_review
    served by GET /jobs/{id}), so receipt counts and API flags agree."""
    return line_needs_review(line, page.get("profile"))


def build_receipt(job: dict, pages: list[dict], reviewer: str | None = None,
                  ocr_engine: str | None = None) -> dict[str, Any]:
    """Processing receipt for one job (all counts derived from the stored
    contract JSON; nothing here mutates the job)."""
    per_page = []
    tot = {"lines": 0, "auto": 0, "human": 0, "corr": 0, "ms": 0}
    tiers: dict[str, int] = {}
    human_verdicts = 0
    stub_pages = 0

    for p in pages:
        lines = p.get("lines") or []
        human = sum(1 for l in lines if _line_needs_human(l, p))
        corrections = p.get("corrections") or []
        for c in corrections:
            t = str(c.get("tier", "?"))
            tiers[t] = tiers.get(t, 0) + 1
        human_verdicts += sum(
            1 for v in (p.get("verdicts") or []) if v.get("source") == "review"
        )
        if any(str(l.get("body", "")).startswith(STUB_MARK) for l in lines):
            stub_pages += 1
        ms = int(p.get("processing_ms") or 0)
        per_page.append({
            "page": p.get("page"),
            "profile": p.get("profile"),
            "needs_review": bool(p.get("needs_review")),
            "lines": len(lines),
            "lines_auto": len(lines) - human,
            "lines_human_review": human,
            "corrections": len(corrections),
            "processing_ms": ms,
        })
        tot["lines"] += len(lines)
        tot["auto"] += len(lines) - human
        tot["human"] += human
        tot["corr"] += len(corrections)
        tot["ms"] += ms

    human_pages = sum(1 for p in per_page if p["needs_review"])
    master = storage.master_path(job["id"])
    return {
        "receipt_version": RECEIPT_VERSION,
        "job_id": job["id"],
        "filename": job["filename"],
        "status": job["status"],
        "master": {
            "sha256": job["sha256"],
            "file": master.name if master else None,
            # every master in page order (1 entry for single-file jobs)
            "files": [m.name for m in storage.master_paths(job["id"])],
            "verified_on_disk": storage.verify_master(job["id"], job["sha256"]),
        },
        "page_count": len(pages),
        "pages": {"auto": len(pages) - human_pages, "human_review": human_pages},
        "lines": {"total": tot["lines"], "auto": tot["auto"],
                  "human_review": tot["human"]},
        "corrections": {"total": tot["corr"], "by_tier": tiers,
                        "human_verdicts": human_verdicts},
        "reviewer": reviewer or None,
        "time": {
            "created_at": job["created_at"],
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "processing_ms_total": tot["ms"],
        },
        "ocr": {"engine_now": ocr_engine, "stub_pages": stub_pages,
                "review_floor": CONFIDENCE_REVIEW_FLOOR},
        "per_page": per_page,
    }


def receipt_lines(r: dict) -> list[str]:
    """Human-readable receipt (used in TXT header and the PDF receipt page)."""
    tiers = ", ".join(f"{k}={v}" for k, v in sorted(r["corrections"]["by_tier"].items())) or "none"
    return [
        "Pink Cloud processing receipt",
        f"Job: {r['job_id']}",
        f"File: {r['filename']}",
        f"Master SHA-256: {r['master']['sha256']}",
        f"Master verified on disk: {'yes' if r['master']['verified_on_disk'] else 'NO'}",
        f"Pages: {r['page_count']} (auto {r['pages']['auto']}, human review {r['pages']['human_review']})",
        f"Lines: {r['lines']['total']} (auto {r['lines']['auto']}, human review {r['lines']['human_review']})",
        f"Corrections: {r['corrections']['total']} (tiers: {tiers}; human verdicts {r['corrections']['human_verdicts']})",
        f"Reviewer: {r['reviewer'] or 'none recorded'}",
        f"Created: {r['time']['created_at']}",
        f"Exported: {r['time']['exported_at']}",
        f"Processing time: {r['time']['processing_ms_total']} ms",
        f"OCR engine (now): {r['ocr']['engine_now'] or 'unknown'}; stub pages: {r['ocr']['stub_pages']}",
    ]


# --------------------------------------------------------------------------
# TXT
# --------------------------------------------------------------------------

def build_txt(pages: list[dict], receipt: dict) -> str:
    out = ["# " + l for l in receipt_lines(receipt)]
    for p in pages:
        out.append("")
        out.append(f"=== page {p.get('page')} ===")
        lines = sorted(p.get("lines") or [], key=lambda l: l.get("seq", 0))
        out.append("\n".join(l["body"] for l in lines) if lines else p.get("text", ""))
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# DOCX
# --------------------------------------------------------------------------

DOCX_TAMIL_FONT = "Noto Sans Tamil"


def _docx_run_font(run, name: str) -> None:
    """Set a run's font for Latin AND complex scripts (Tamil is w:cs), so
    Word/LibreOffice pick a Tamil-capable face instead of a fallback box."""
    from docx.oxml.ns import qn
    run.font.name = name
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rfonts.set(qn(attr), name)


def build_docx(pages: list[dict], receipt: dict) -> bytes:
    """Word export: title, one 'Page N' heading per page with the
    (corrections-applied) line text in reading order, then the receipt as
    the final section. Tamil text is written as-is (Unicode); layout and
    shaping are left to the word processor."""
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    doc.core_properties.title = f"Pink Cloud export - {receipt.get('filename', '')}"
    doc.core_properties.keywords = "master-sha256:" + receipt["master"]["sha256"]
    doc.core_properties.comments = "Generated by Pink Cloud (Tamil OCR)"
    doc.add_heading("Pink Cloud export", level=0)
    for p in pages:
        doc.add_heading(f"Page {p.get('page')}", level=1)
        lines = sorted(p.get("lines") or [], key=lambda l: l.get("seq", 0))
        bodies = [l["body"] for l in lines] if lines else (p.get("text") or "").splitlines()
        if not bodies:
            doc.add_paragraph("(no text)")
        for body in bodies:
            run = doc.add_paragraph().add_run(body)
            _docx_run_font(run, DOCX_TAMIL_FONT)
            run.font.size = Pt(12)
    doc.add_heading("Processing receipt", level=1)
    for line in receipt_lines(receipt)[1:]:  # [0] is the title, now a heading
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------

def _wide(text: str):
    """Python str -> FPDF_WIDESTRING (UTF-16LE, NUL-terminated). The buffer
    is returned too so it outlives the call."""
    import pypdfium2.raw as r
    buf = ctypes.create_string_buffer((text + "\x00").encode("utf-16-le"))
    return buf, ctypes.cast(buf, ctypes.POINTER(r.FPDF_WCHAR))


def _add_text(pdf_raw, page_raw, font, text: str, size_pt: float,
              x_pt: float, y_pt: float, width_pt: float | None,
              invisible: bool) -> None:
    import pypdfium2.raw as r
    obj = r.FPDFPageObj_CreateTextObj(pdf_raw, font, ctypes.c_float(size_pt))
    _buf, ws = _wide(text)
    r.FPDFText_SetText(obj, ws)
    if invisible:
        r.FPDFTextObj_SetTextRenderMode(obj, r.FPDF_TEXTRENDERMODE_INVISIBLE)
    sx = 1.0
    if width_pt:
        l, b, rt, t = (ctypes.c_float() for _ in range(4))
        r.FPDFPageObj_GetBounds(obj, ctypes.byref(l), ctypes.byref(b),
                                ctypes.byref(rt), ctypes.byref(t))
        natural = rt.value - l.value
        if natural > 0:
            sx = width_pt / natural
    r.FPDFPageObj_Transform(obj, sx, 0, 0, 1, x_pt, y_pt)
    r.FPDFPage_InsertObject(page_raw, obj)


def _load_font(pdf_raw, data: bytes):
    import pypdfium2.raw as r
    arr = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
    font = r.FPDFText_LoadFont(pdf_raw, arr, len(data), r.FPDF_FONT_TRUETYPE, True)
    if not font:
        raise RuntimeError("PDFium could not load the Tamil font")
    return font, arr


def _pdf_str(s: str) -> str:
    """Unicode PDF text string as <FEFF...> hex (UTF-16BE with BOM)."""
    return "<FEFF" + s.encode("utf-16-be").hex().upper() + ">"


def _add_info_dict(pdf_bytes: bytes, info: dict[str, str]) -> bytes:
    """Append an incremental update that adds a /Info dictionary carrying
    provenance (PDFium has no metadata setter). Original bytes untouched."""
    m = list(re.finditer(rb"startxref\s+(\d+)", pdf_bytes))
    root = re.findall(rb"/Root\s+(\d+)\s+(\d+)\s+R", pdf_bytes)
    size = re.findall(rb"/Size\s+(\d+)", pdf_bytes)
    if not m or not root or not size:
        return pdf_bytes  # unknown layout: skip metadata rather than corrupt
    prev = int(m[-1].group(1))
    obj_no = int(size[-1])
    body = "<<" + "".join(f"/{k} {_pdf_str(v)}" for k, v in info.items()) + ">>"
    base = pdf_bytes if pdf_bytes.endswith(b"\n") else pdf_bytes + b"\n"
    offset = len(base)
    obj = f"{obj_no} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref_at = offset + len(obj)
    xref = (f"xref\n{obj_no} 1\n{offset:010d} 00000 n \n"
            f"trailer\n<</Size {obj_no + 1}/Root {root[-1][0].decode()} "
            f"{root[-1][1].decode()} R/Info {obj_no} 0 R/Prev {prev}>>\n"
            f"startxref\n{xref_at}\n%%EOF\n").encode("latin-1")
    return base + obj + xref


# PDFium is not thread-safe and sync routes run in a thread pool: two
# concurrent exports (or an export racing an upload / page render) corrupt
# the native heap and kill the server. Share pdfutil's lock when it exists,
# otherwise create it there so every pypdfium2 user in the app shares one.
_PDFIUM_LOCK = getattr(_pdfutil, "PDFIUM_LOCK", None)
if _PDFIUM_LOCK is None:
    _PDFIUM_LOCK = _pdfutil.PDFIUM_LOCK = threading.RLock()


def build_pdf(master: Path | list[Path], pages: list[dict], receipt: dict,
              receipt_page: bool = True) -> bytes:
    with _PDFIUM_LOCK:
        return _build_pdf(master, pages, receipt, receipt_page)


def _build_pdf(master: Path | list[Path], pages: list[dict], receipt: dict,
               receipt_page: bool) -> bytes:
    import pypdfium2 as pdfium
    import pypdfium2.raw as r
    from PIL import Image

    from .pdfutil import load_pages

    images = load_pages(master)  # same 1600px-capped images the bboxes use
    by_no = {int(p.get("page", i + 1)): p for i, p in enumerate(pages)}

    pdf = pdfium.PdfDocument.new()
    font, _font_buf = _load_font(pdf.raw, FONT_PATH.read_bytes())
    helv = r.FPDFText_LoadStandardFont(pdf.raw, b"Helvetica")
    keep = []  # keep JPEG buffers alive until save
    try:
        for i, img in enumerate(images, start=1):
            h_px, w_px = img.shape[:2]
            w_pt, h_pt = w_px * PT_PER_PX, h_px * PT_PER_PX
            page = pdf.new_page(w_pt, h_pt)

            jpg = io.BytesIO()
            Image.fromarray(img[:, :, ::-1]).save(jpg, "JPEG", quality=88)
            jpg.seek(0)
            keep.append(jpg)
            pimg = pdfium.PdfImage.new(pdf)
            pimg.load_jpeg(jpg, pages=[page], inline=True)
            pimg.set_matrix(pdfium.PdfMatrix().scale(w_pt, h_pt))
            page.insert_obj(pimg)

            for line in (by_no.get(i) or {}).get("lines") or []:
                body = str(line.get("body", "")).strip()
                if not body or body.startswith(STUB_MARK):
                    continue
                x, y, bw, bh = (float(v) for v in line["bbox"])
                size = max(bh * PT_PER_PX * 0.8, 1.0)
                baseline = h_pt - (y + bh) * PT_PER_PX + bh * PT_PER_PX * 0.2
                _add_text(pdf.raw, page.raw, font, body, size,
                          x * PT_PER_PX, baseline, bw * PT_PER_PX, invisible=True)
            page.gen_content()

        if receipt_page:  # visible, searchable provenance page at the end
            page = pdf.new_page(595, 842)
            y = 800.0
            for k, line in enumerate(receipt_lines(receipt)):
                size = 16 if k == 0 else 10
                _add_text(pdf.raw, page.raw, helv, line, size, 50, y, None,
                          invisible=False)
                y -= 26 if k == 0 else 16
            page.gen_content()

        out = io.BytesIO()
        pdf.save(out)
    finally:
        r.FPDFFont_Close(font)
        if helv:
            r.FPDFFont_Close(helv)
        pdf.close()

    return _add_info_dict(out.getvalue(), {
        "Title": f"{receipt['filename']} (Pink Cloud OCR)",
        "Producer": "Pink Cloud export (pypdfium2)",
        "Subject": "Searchable Tamil OCR export with processing receipt",
        "Keywords": f"master-sha256:{receipt['master']['sha256']} job:{receipt['job_id']}",
        "PinkCloudJobId": receipt["job_id"],
        "PinkCloudMasterSHA256": receipt["master"]["sha256"],
        "PinkCloudReceipt": json.dumps(receipt, ensure_ascii=False,
                                       separators=(",", ":")),
    })
