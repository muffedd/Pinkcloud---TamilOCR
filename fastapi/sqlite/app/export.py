"""Export + processing receipt for Pink Cloud (slice: pipe-export-receipt).

Three outputs for a finished job:
  - build_receipt()  -> dict: page count, auto vs human-review counts,
                        corrections, reviewer, times, master SHA-256
                        (re-verified against the stored master on disk).
  - build_txt()      -> str: ONLY the transcribed text, page by page in
                        reading order (a "=== page N ===" separator per
                        page on multi-page jobs). No header, no receipt.
  - build_docx()     -> bytes: Word document with the same text as
                        build_txt() ("Page N" heading per page on
                        multi-page jobs). No title block, no receipt
                        section. Needs python-docx.
  - build_pdf()      -> bytes: text-first PDF. It opens with the recognized
                        (corrections-applied) lines as VISIBLE, readable
                        Tamil text pages - one A4 page per source page, the
                        text shrunk/reflowed to fit it - then (unless
                        include_scans=False) the scan pages, each with an
                        INVISIBLE text layer (one text object per OCR line,
                        placed and stretched over the line bbox, the
                        hOCR-to-PDF idea). No receipt page: the receipt lives
                        at GET /jobs/{id}/receipt. Job id + master SHA-256
                        go in the PDF Info dictionary.

Offline, pinned deps only: pypdfium2 (PDFium page-object API) + Pillow.
The Tamil text layer uses the bundled fonts/noto-sans-tamil.ttf, embedded
as a CID font so PDFium writes a ToUnicode map -> Ctrl+F and pdftotext
return the real Unicode Tamil text. PDFium's writer does not shape
glyphs, which does not matter for the invisible layers (search / copy
only). The VISIBLE text pages are shaped with HarfBuzz (uharfbuzz):
each line is drawn as filled glyph outlines (vector paths, so vowel signs
and conjuncts come out right), with an invisible Unicode text object over
it so the same line is still extractable, searchable and copyable.

Bboxes in the page JSON are on the 1600px-capped image from
pdfutil.load_pages(), so we embed exactly that image and map
1 px -> PT_PER_PX points (the capped image is treated as 150 dpi).
"""

from __future__ import annotations

import ctypes
import io
import json
import re
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import storage
from .pdfutil import PDFIUM_LOCK as _PDFIUM_LOCK
from .schema_out import LINE_REVIEW_FLOOR, STUB_MARK, line_needs_review

PT_PER_PX = 72.0 / 150.0
FONT_PATH = Path(__file__).resolve().parents[3] / "fonts" / "noto-sans-tamil.ttf"
RECEIPT_VERSION = 1

_log = logging.getLogger("pinkcloud.export")


# --------------------------------------------------------------------------
# saved reviewer corrections (uploads/<job_id>/corrections.json)
# --------------------------------------------------------------------------

CORRECTIONS_FILE = "corrections.json"


def read_saved_corrections(job_id: str) -> tuple[list[dict], str | None]:
    """Read the job's saved correction map written by the corrections route.

    Shape: {"corrections": [{page, line, word, before, after}, ...], ...}.
    Returns (usable corrections, problem). A missing file is not a problem
    (no corrections yet). An unreadable / non-JSON / wrongly shaped file, or
    malformed entries, give a one-line problem string so the receipt can
    say corrections were NOT (all) applied instead of quietly showing 0."""
    path = storage.UPLOAD_ROOT / job_id / CORRECTIONS_FILE
    if not path.is_file():
        return [], None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # unreadable, not JSON
        _log.warning("corrections for job %s unreadable: %s", job_id, exc)
        return [], f"saved corrections file is unreadable ({type(exc).__name__})"
    items = doc.get("corrections") if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        _log.warning("corrections for job %s have the wrong shape", job_id)
        return [], "saved corrections file has the wrong shape"
    out = []
    skipped = 0
    for c in items:
        try:
            page, word = int(c["page"]), int(c["word"])
            line, after = str(c["line"]), str(c["after"])
        except Exception:
            skipped += 1
            continue
        before = c.get("before")
        if page < 1 or word < 1 or not after.strip():
            skipped += 1
            continue
        out.append({"page": page, "line": line, "word": word,
                    "before": None if before is None else str(before),
                    "after": after.strip()})
    problem = (f"{skipped} of {len(items)} saved corrections were malformed and skipped"
               if skipped else None)
    return out, problem


def load_saved_corrections(job_id: str) -> list[dict]:
    """Usable saved corrections ([] when none or unreadable); see
    read_saved_corrections for the problem report."""
    return read_saved_corrections(job_id)[0]


def saved_corrections_error(job_id: str) -> str | None:
    """Why saved corrections could not be (fully) applied, or None."""
    return read_saved_corrections(job_id)[1]


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
    """pages with the job's saved corrections applied (raw pages on any error;
    the receipt reports the problem via saved_corrections_error)."""
    try:
        return apply_corrections(pages, load_saved_corrections(job_id))
    except Exception:
        _log.exception("applying saved corrections failed for job %s", job_id)
        return pages


def count_applicable_corrections(job_id: str, pages: list[dict]) -> int:
    """How many of the job's saved corrections still apply to the CURRENT
    OCR text - the same still-applies rule as apply_corrections (a stale
    correction whose `before` no longer matches the word is skipped, and a
    word already changed by a later correction counts only the winner).
    Powers GET /jobs' corrections_count, so the Library shows the fixes a
    reviewer would actually see applied, not the raw corrections-array
    length. Stored pages never carry tier "human" (the contract tiers are
    T1/T2), so every "human" entry below came from this application."""
    applied = apply_saved_corrections(job_id, pages)
    return sum(
        1 for p in applied for c in (p.get("corrections") or [])
        if c.get("tier") == "human"
    )


# --------------------------------------------------------------------------
# receipt
# --------------------------------------------------------------------------

def _line_needs_human(line: dict, page: dict) -> bool:
    """A line goes to a human if its text-quality confidence is under the
    line review floor (text looks malformed) or it is stub output. A HEAVY
    page no longer sends every line. The rule itself lives
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
        # null when fine; else why saved corrections were NOT (all) applied
        "corrections_error": saved_corrections_error(job["id"]),
        "reviewer": reviewer or None,
        "time": {
            "created_at": job["created_at"],
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "processing_ms_total": tot["ms"],
        },
        "ocr": {"engine_now": ocr_engine, "stub_pages": stub_pages,
                "review_floor": LINE_REVIEW_FLOOR},
        "per_page": per_page,
    }


def receipt_lines(r: dict) -> list[str]:
    """Human-readable receipt lines (no export uses them any more; kept for
    callers that want a plain-text rendering of GET /jobs/{id}/receipt)."""
    tiers = ", ".join(f"{k}={v}" for k, v in sorted(r["corrections"]["by_tier"].items())) or "none"
    warn = ([f"WARNING - corrections not fully applied: {r['corrections_error']}"]
            if r.get("corrections_error") else [])
    return [
        "Pink Cloud processing receipt",
        f"Job: {r['job_id']}",
        f"File: {r['filename']}",
        f"Master SHA-256: {r['master']['sha256']}",
        f"Master verified on disk: {'yes' if r['master']['verified_on_disk'] else 'NO'}",
        f"Pages: {r['page_count']} (auto {r['pages']['auto']}, human review {r['pages']['human_review']})",
        f"Lines: {r['lines']['total']} (auto {r['lines']['auto']}, human review {r['lines']['human_review']})",
        f"Corrections: {r['corrections']['total']} (tiers: {tiers}; human verdicts {r['corrections']['human_verdicts']})",
        *warn,
        f"Reviewer: {r['reviewer'] or 'none recorded'}",
        f"Created: {r['time']['created_at']}",
        f"Exported: {r['time']['exported_at']}",
        f"Processing time: {r['time']['processing_ms_total']} ms",
        f"OCR engine (now): {r['ocr']['engine_now'] or 'unknown'}; stub pages: {r['ocr']['stub_pages']}",
    ]


# --------------------------------------------------------------------------
# TXT
# --------------------------------------------------------------------------

def _export_bodies(p: dict) -> list[str]:
    """Line bodies of one page in reading order, WITHOUT "[stub]" marker
    lines (same rule as the PDF text layer). The receipt still counts
    stub pages, so the marker is never hidden from the reviewer."""
    lines = sorted(p.get("lines") or [], key=lambda l: l.get("seq", 0))
    bodies = ([str(l.get("body", "")) for l in lines] if lines
              else (p.get("text") or "").splitlines())
    return [b for b in bodies if not b.strip().startswith(STUB_MARK)]


def _page_order(pages: list[dict]) -> list[dict]:
    return sorted(pages, key=lambda p: int(p.get("page") or 0))


def build_txt(pages: list[dict], receipt: dict | None = None) -> str:
    """Plain-text export: ONLY the transcribed (corrections-applied) text.
    Single-page jobs are just the lines; multi-page jobs get a
    "=== page N ===" separator before each page. `receipt` is accepted for
    old callers and ignored: the receipt lives at GET /jobs/{id}/receipt."""
    pages = _page_order(pages)
    multi = len(pages) > 1
    out: list[str] = []
    for p in pages:
        if multi:
            if out:
                out.append("")
            out.append(f"=== page {p.get('page')} ===")
        out.extend(_export_bodies(p))
    return ("\n".join(out) + "\n") if out else ""


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


def build_docx(pages: list[dict], receipt: dict | None = None, *,
               filename: str | None = None, sha256: str | None = None) -> bytes:
    """Word export: ONLY the transcribed (corrections-applied) line text in
    reading order, with a "Page N" heading per page on multi-page jobs. No
    title block and no receipt section (GET /jobs/{id}/receipt has that).
    Filename + master SHA-256 go in the document properties only (like the
    PDF Info dict); `receipt` is accepted for old callers as a fallback
    source for them. Tamil text is written as-is (Unicode); layout and
    shaping are left to the word processor."""
    from docx import Document
    from docx.shared import Pt

    receipt = receipt or {}
    filename = filename if filename is not None else receipt.get("filename", "")
    sha256 = sha256 if sha256 is not None else (receipt.get("master") or {}).get("sha256")

    doc = Document()
    doc.core_properties.title = f"Pink Cloud export - {filename or ''}"
    if sha256:
        doc.core_properties.keywords = "master-sha256:" + sha256
    doc.core_properties.comments = "Generated by Pink Cloud (Tamil OCR)"

    def para(text: str):
        run = doc.add_paragraph().add_run(text)
        _docx_run_font(run, DOCX_TAMIL_FONT)
        run.font.size = Pt(12)

    pages = _page_order(pages)
    multi = len(pages) > 1
    any_text = False
    for p in pages:
        bodies = _export_bodies(p)
        if multi:
            doc.add_heading(f"Page {p.get('page')}", level=1)
            if not bodies:
                doc.add_paragraph("(no text)")
        for body in bodies:
            para(body)
            any_text = True
    if not any_text and not multi:
        doc.add_paragraph(EMPTY_TEXT_NOTE)
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
# the native heap and kill the server. _PDFIUM_LOCK is pdfutil's lock, so
# every pypdfium2 user in the app shares one.


def build_pdf(master: Path | list[Path], pages: list[dict], receipt: dict,
              receipt_page: bool = False, include_scans: bool = True) -> bytes:
    """receipt_page is accepted for old callers and ignored: the PDF never
    carries a receipt page any more (GET /jobs/{id}/receipt has the data).

    include_scans=False drops the scan pages: the PDF is only the visible
    text pages (still one per source page, each with its invisible Unicode
    layer, so it stays searchable). Default True keeps the old output."""
    with _PDFIUM_LOCK:
        return _build_pdf(master, pages, receipt, include_scans=include_scans)


# ---- visible text pages ---------------------------------------------------
#
# One visible text page per source page: each scan page's recognized text is
# fitted onto its own A4 text page instead of
# flowing continuously, so text page N always matches scan page N. Fitting
# keeps every OCR line as its own line (reflowed inside the page width when
# it is too long) and shrinks the font from TEXT_SIZE down to FIT_MIN_SIZE
# until the whole page's text fits. Only when even FIT_MIN_SIZE overflows
# (a very dense page) does the text continue on a "(continued)" page, so
# nothing is ever clipped.

TEXT_PAGE_W, TEXT_PAGE_H = 595.0, 842.0   # A4 portrait, points
TEXT_MARGIN = 56.0
TEXT_SIZE = 13.0                           # largest (natural) text size
FIT_MIN_SIZE = 5.0                         # smallest size before continuing
LEADING_RATIO = 1.7                        # line pitch / font size at >= 10pt
                                           # (Tamil vowel signs need room)
MIN_LEADING_RATIO = 1.45                   # tighter pitch at FIT_MIN_SIZE
TEXT_LEADING = TEXT_SIZE * LEADING_RATIO


def leading_for(size: float) -> float:
    """Line pitch for a font size: LEADING_RATIO at 10pt and up, easing to
    MIN_LEADING_RATIO at FIT_MIN_SIZE so dense pages still fit legibly."""
    if size >= 10.0:
        return size * LEADING_RATIO
    t = max(0.0, (size - FIT_MIN_SIZE) / (10.0 - FIT_MIN_SIZE))
    return size * (MIN_LEADING_RATIO + t * (LEADING_RATIO - MIN_LEADING_RATIO))
LABEL_SIZE = 9.0
LABEL_GAP = LABEL_SIZE * 2.2               # label line + space under it
EMPTY_TEXT_NOTE = "No text was recognized in this document."
EMPTY_PAGE_NOTE = "No text was recognized on this page."


def _hb_font(data: bytes):
    import uharfbuzz as hb
    face = hb.Face(data)
    return hb.Font(face), face.upem


def _shape(hb_font, text: str):
    import uharfbuzz as hb
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(hb_font, buf, {})
    return buf.glyph_infos, buf.glyph_positions


_WIDTH_CACHE: dict[tuple[int, str], int] = {}


def _units_width(hb_font, text: str) -> int:
    """Shaped advance width in font units (size independent, so it is
    cached and reused across every candidate size tried while fitting)."""
    key = (id(hb_font), text)
    w = _WIDTH_CACHE.get(key)
    if w is None:
        if len(_WIDTH_CACHE) > 50000:
            _WIDTH_CACHE.clear()
        _, pos = _shape(hb_font, text)
        w = _WIDTH_CACHE[key] = sum(p.x_advance for p in pos)
    return w


def _shaped_width(hb_font, upem: int, text: str, size: float) -> float:
    return _units_width(hb_font, text) * size / upem


class _PathPen:
    """fontTools-style pen that writes glyph outlines into one PDFium path
    object, mapping font units -> page points (scale + origin)."""

    def __init__(self, path, scale: float, ox: float, oy: float):
        self.path, self.k, self.ox, self.oy = path, scale, ox, oy
        self.cur = (0.0, 0.0)

    def _pt(self, p):
        return self.ox + p[0] * self.k, self.oy + p[1] * self.k

    def moveTo(self, p):
        import pypdfium2.raw as r
        r.FPDFPath_MoveTo(self.path, *self._pt(p))
        self.cur = p

    def lineTo(self, p):
        import pypdfium2.raw as r
        r.FPDFPath_LineTo(self.path, *self._pt(p))
        self.cur = p

    def curveTo(self, *pts):
        import pypdfium2.raw as r
        c1, c2, end = pts[-3], pts[-2], pts[-1]
        r.FPDFPath_BezierTo(self.path, *self._pt(c1), *self._pt(c2), *self._pt(end))
        self.cur = end

    def qCurveTo(self, *pts):
        # TrueType quadratic run: off-curve points with implied on-curve
        # midpoints; each quad goes out as an exact cubic.
        *offs, end = pts
        if not offs:
            return self.lineTo(end)
        for i, c in enumerate(offs):
            nxt = end if i == len(offs) - 1 else (
                (c[0] + offs[i + 1][0]) / 2, (c[1] + offs[i + 1][1]) / 2)
            p0 = self.cur
            c1 = (p0[0] + 2 / 3 * (c[0] - p0[0]), p0[1] + 2 / 3 * (c[1] - p0[1]))
            c2 = (nxt[0] + 2 / 3 * (c[0] - nxt[0]), nxt[1] + 2 / 3 * (c[1] - nxt[1]))
            self.curveTo(c1, c2, nxt)

    def closePath(self):
        import pypdfium2.raw as r
        r.FPDFPath_Close(self.path)

    def endPath(self):
        pass


def _draw_shaped_line(pdf_raw, page_raw, font, hb_font, upem: int, text: str,
                      size: float, x: float, y: float) -> None:
    """Visible shaped glyph outlines + an invisible Unicode text object over
    the same span (so extraction / search / copy return the real text)."""
    import pypdfium2.raw as r
    infos, pos = _shape(hb_font, text)
    k = size / upem
    path = r.FPDFPageObj_CreateNewPath(ctypes.c_float(x), ctypes.c_float(y))
    pen_x = 0.0
    for info, p in zip(infos, pos):
        pen = _PathPen(path, k, x + (pen_x + p.x_offset) * k, y + p.y_offset * k)
        hb_font.draw_glyph_with_pen(info.codepoint, pen)
        pen_x += p.x_advance
    r.FPDFPageObj_SetFillColor(path, 20, 20, 20, 255)
    r.FPDFPath_SetDrawMode(path, r.FPDF_FILLMODE_WINDING, False)
    r.FPDFPage_InsertObject(page_raw, path)
    width = pen_x * k
    _add_text(pdf_raw, page_raw, font, text, size, x, y,
              width if width > 0 else None, invisible=True)


def _wrap(hb_font, upem: int, text: str, size: float, max_w: float) -> list[str]:
    """Greedy word wrap on shaped widths. A single word wider than the line
    stays whole on its own line (never split inside a Tamil cluster); the
    fitter shrinks the font until such words fit where it can."""
    words = text.split()
    out, cur = [], ""
    for w in words:
        cand = f"{cur} {w}" if cur else w
        if cur and _shaped_width(hb_font, upem, cand, size) > max_w:
            out.append(cur)
            cur = w
        else:
            cur = cand
    if cur:
        out.append(cur)
    return out


def _layout(hb_font, upem: int, bodies: list[str], size: float,
            max_w: float) -> tuple[list[str], bool]:
    """Visual lines for one source page at `size` (each OCR line starts a
    new line, then wraps), plus whether every line fits the width."""
    vis: list[str] = []
    fits_w = True
    for b in bodies:
        parts = _wrap(hb_font, upem, b, size, max_w) or [""]
        for part in parts:
            if part and _shaped_width(hb_font, upem, part, size) > max_w + 0.01:
                fits_w = False
        vis.extend(parts)
    return vis, fits_w


def fit_text_to_page(hb_font, upem: int, bodies: list[str], avail_w: float,
                     avail_h: float) -> tuple[float, list[str]]:
    """Largest font size in [FIT_MIN_SIZE, TEXT_SIZE] at which `bodies`
    (wrapped to avail_w) fit inside avail_h, and the wrapped lines at that
    size. Returns FIT_MIN_SIZE when nothing in range fits (caller then
    continues onto another page). Height model: the first baseline sits one
    `size` below the top, every further line one leading_for(size) lower, and the last
    line keeps a descender's worth (0.3 * size) above the bottom margin."""

    def fits(size: float):
        vis, fits_w = _layout(hb_font, upem, bodies, size, avail_w)
        need = size + (len(vis) - 1) * leading_for(size) + 0.3 * size
        return fits_w and need <= avail_h, vis

    ok, vis = fits(TEXT_SIZE)
    if ok:
        return TEXT_SIZE, vis
    lo, hi = FIT_MIN_SIZE, TEXT_SIZE  # invariant: hi does not fit
    ok, lo_vis = fits(lo)
    if not ok:
        return FIT_MIN_SIZE, lo_vis
    for _ in range(12):  # ~0.002pt resolution
        mid = (lo + hi) / 2
        ok, mid_vis = fits(mid)
        if ok:
            lo, lo_vis = mid, mid_vis
        else:
            hi = mid
    size = int(lo * 4) / 4.0  # snap down to a quarter point
    if size < FIT_MIN_SIZE:
        size = FIT_MIN_SIZE
    ok, vis = fits(size)
    return (size, vis) if ok else (lo, lo_vis)


def _source_bodies(pages: list[dict], n_pages: int) -> list[list[str]]:
    by_no = {int(p.get("page", i + 1)): p for i, p in enumerate(pages)}
    return [_export_bodies(by_no.get(n) or {}) for n in range(1, n_pages + 1)]


def _add_text_pages(pdf, font, helv, hb_font, upem: int, pages: list[dict],
                    images: list) -> int:
    """Visible retyped-text pages: one per source page, each fitted to its
    page (see fit_text_to_page). Returns how many pages were added."""
    n_pages = len(images)
    per_page = _source_bodies(pages, n_pages)
    empty_doc = not any(per_page)
    labelled = n_pages > 1
    added = 0

    for n, bodies in enumerate(per_page, start=1):
        pw, ph = TEXT_PAGE_W, TEXT_PAGE_H
        avail_w = pw - 2 * TEXT_MARGIN
        top = ph - TEXT_MARGIN
        if empty_doc:
            bodies = [EMPTY_TEXT_NOTE] if n == 1 else []
        elif not bodies:
            bodies = [EMPTY_PAGE_NOTE]
        label_h = LABEL_GAP if labelled else 0.0
        if not bodies:  # empty doc: pages after the first just carry a label
            bodies = [""]
        size, vis = fit_text_to_page(hb_font, upem, bodies, avail_w,
                                     top - label_h - TEXT_MARGIN)
        leading = leading_for(size)
        part = 0
        i = 0
        while True:
            page = pdf.new_page(pw, ph)
            added += 1
            y = top
            if labelled or part:
                label = f"Page {n}" if labelled else ""
                if part:
                    label = (label + " (continued)").strip()
                y -= LABEL_SIZE
                _add_text(pdf.raw, page.raw, helv, label, LABEL_SIZE,
                          TEXT_MARGIN, y, None, invisible=False)
                y = top - LABEL_GAP
            y -= size  # first baseline
            while i < len(vis) and y >= TEXT_MARGIN + 0.3 * size - 0.01:
                if vis[i]:
                    _draw_shaped_line(pdf.raw, page.raw, font, hb_font, upem,
                                      vis[i], size, TEXT_MARGIN, y)
                y -= leading
                i += 1
            page.gen_content()
            if i >= len(vis):
                break
            part += 1
    return added


def _build_pdf(master: Path | list[Path], pages: list[dict],
               receipt: dict, include_scans: bool = True) -> bytes:
    import pypdfium2 as pdfium
    import pypdfium2.raw as r
    from PIL import Image

    from .pdfutil import load_pages

    images = load_pages(master)  # same 1600px-capped images the bboxes use
    if not images:
        # Never ship a PDF without the document: fail loudly instead.
        raise RuntimeError("no page images for the export PDF")
    by_no = {int(p.get("page", i + 1)): p for i, p in enumerate(pages)}

    pdf = pdfium.PdfDocument.new()
    font_data = FONT_PATH.read_bytes()
    font, _font_buf = _load_font(pdf.raw, font_data)
    hb_font, upem = _hb_font(font_data)
    helv = r.FPDFText_LoadStandardFont(pdf.raw, b"Helvetica")
    keep = []  # keep JPEG buffers alive until save
    try:
        # 1) visible retyped text first: page 1 shows the document's content,
        #    one text page per source page, each fitted to its page
        _add_text_pages(pdf, font, helv, hb_font, upem, pages, images)

        # 2) the scans, each with its invisible (searchable) text layer
        #    (skipped when the user exported without the original scans)
        for i, img in enumerate(images if include_scans else [], start=1):
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

        out = io.BytesIO()
        pdf.save(out)
    finally:
        r.FPDFFont_Close(font)
        if helv:
            r.FPDFFont_Close(helv)
        pdf.close()

    return _add_info_dict(out.getvalue(), {
        "Title": f"{receipt['filename']} (Pink Cloud OCR)",
        "Producer": "Pink Cloud export (pypdfium2 + HarfBuzz)",
        "Subject": ("Tamil OCR text followed by the searchable scan"
                    if include_scans else "Tamil OCR text (scan pages not included)"),
        "Keywords": f"master-sha256:{receipt['master']['sha256']} job:{receipt['job_id']}",
        "PinkCloudJobId": receipt["job_id"],
        "PinkCloudMasterSHA256": receipt["master"]["sha256"],
    })
