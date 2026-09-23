"""Pink Cloud — Tamil OCR web app backend (pipe-skeleton-router).

Pipeline per upload:
  1. validate type AND extension -> main.py   (both required; 400 before
     any job row exists; undecodable bytes -> 4xx, never a failed job)
  2. hash + save master   -> storage.py  (byte-for-byte, SHA-256 first)
  3. create job row       -> db.py
  4. load pages           -> pdfutil.py  (PDF via pypdfium2, images via
     cv2, ALL pages of multi-page TIFFs)
  5. score + route        -> router.py   (FAST / HEAVY badge per page)
  6. OCR fast pass        -> ocr.py      (lazy PaddleOCR 3.x, marked stub
     fallback; engine failures are logged and shown in /health)
  7. build contract JSON  -> schema_out.py
  8. store result         -> db.py

Run:  uvicorn app.main:app --reload
Docs: see RUN.md
"""

import json
import logging
import re
import time
import uuid
from pathlib import Path

import cv2
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from . import db, storage
from .ocr import engine_status, failed_page_lines, ocr_page, _get_engine
from .pdfutil import load_pages, probe_decode, to_gray
from .router import choose_profile, compute_scores
from .schema_out import (build_job_result, build_page_result, enrich_page,
                         parse_job_result)

# Both a supported content type AND a supported extension are required;
# a mismatch on EITHER side is rejected with 400 before any job exists.
ALLOWED_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/webp",
}

# Multi-image jobs (repeated `files` field): images only, one page each.
MAX_FILES_PER_JOB = 100

# Upload caps (demo-safe). Every page is rendered into RAM at ~200 DPI, so
# both the bytes and the PDF page count are bounded BEFORE any job exists.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024   # one upload (or all `files` together)
MAX_PDF_PAGES = 25
# Multipart framing overhead allowed on top of MAX_UPLOAD_BYTES when the
# request's Content-Length is checked up front.
_MULTIPART_SLACK_BYTES = 1024 * 1024

app = FastAPI(title="Pink Cloud", version="0.2.0")


def _too_large_detail() -> str:
    return f"upload too large: at most {MAX_UPLOAD_BYTES // (1024 * 1024)} MB per job"


@app.middleware("http")
async def _cap_upload_body(request, call_next):
    """413 an oversized POST /jobs from its Content-Length, before the
    multipart body is parsed. Uploads without a Content-Length are still
    capped after reading (see _read_capped)."""
    if request.method == "POST" and request.url.path.rstrip("/") == "/jobs":
        try:
            length = int(request.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length > MAX_UPLOAD_BYTES + _MULTIPART_SLACK_BYTES:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=413,
                                content={"detail": _too_large_detail()})
    return await call_next(request)


def _read_capped(f: UploadFile, budget: int) -> bytes:
    """Read one upload, 413 if it exceeds the remaining byte budget."""
    data = f.file.read(budget + 1)
    if len(data) > budget:
        raise HTTPException(status_code=413, detail=_too_large_detail())
    return data


@app.on_event("startup")
def on_startup() -> None:
    """Create the SQLite table and probe the OCR engine at boot.

    The engine probe is non-fatal: a missing/broken paddle logs an
    exception and /health reports the stub engine — the API still works.
    """
    logging.basicConfig(level=logging.INFO)
    db.init_db()
    _get_engine()  # eager init so /health reflects reality from the start


@app.get("/health")
def health():
    """Liveness check. Also reports the OCR engine state so stub output
    is never mistaken for real OCR text."""
    return {"ok": True, **engine_status()}


def _pipeline(job_id: str, master: Path | list[Path]) -> list[dict]:
    """Process the master file(s) -> list of per-page contract JSON objects."""
    pages_out: list[dict] = []

    # (4) Load every page as a 1600px-capped BGR image.
    for page_number, img in enumerate(load_pages(master), start=1):
        t_page = time.perf_counter()
        gray = to_gray(img)

        # (5) Quality metrics + FAST/HEAVY badge. HEAVY pages would
        #     normally go to a repair pass first — the skeleton just
        #     OCR's them directly for now.
        scores = compute_scores(gray)
        profile, scores = choose_profile(scores)

        # (6) OCR fast pass (clearly-marked stub lines if paddle missing).
        #     A failure on ONE page (Sarvam + paddle fallback, or paddle
        #     alone) must not sink the job: that page gets a marked stub
        #     line (-> needs_review) and the other pages still run.
        try:
            ocr_lines, ocr_ms = ocr_page(img)
        except Exception as exc:
            logging.getLogger("pinkcloud.job").exception(
                "job %s: OCR failed on page %d", job_id, page_number)
            ocr_lines, ocr_ms = failed_page_lines(img, exc), 0.0

        # (7) Freeze the contract JSON for this page.
        page_ms = (time.perf_counter() - t_page) * 1000.0
        pages_out.append(
            build_page_result(
                page_number=page_number,
                profile=profile,
                quality={
                    "blur": scores.blur,
                    "contrast": scores.contrast,
                    "noise": scores.noise,
                    "skew_deg": scores.skew_deg,
                },
                ocr_lines=ocr_lines,
                processing_ms=page_ms,
            )
        )

    return pages_out


def _run_multi_job(job_id: str, uploads: list[tuple[str, bytes]],
                   expected_sha256: str) -> None:
    """Multi-image flow: one master per image, pages stacked in order."""
    try:
        saved = storage.save_masters(job_id, uploads)
        if saved["sha256"] != expected_sha256 or not storage.verify_master(
                job_id, expected_sha256):
            raise RuntimeError("master files failed hash verification")
        pages = _pipeline(job_id, saved["paths"])
        result = build_job_result(pages)
        db.set_result(job_id, "done", json.dumps(result, ensure_ascii=False))
    except Exception as exc:  # keep the server alive, record the failure
        logging.getLogger("pinkcloud.job").exception("job %s failed", job_id)
        db.set_result(job_id, "error", json.dumps({"error": str(exc)}))


def _run_job(job_id: str, filename: str, data: bytes) -> None:
    """The whole upload-to-result flow, wrapped so errors land in the DB."""
    try:
        # (2) Hash BEFORE writing, store master byte-for-byte.
        saved = storage.save_master(job_id, filename, data)
        master = Path(saved["path"])

        # Cheap integrity check immediately after writing.
        if not storage.verify_master(job_id, saved["sha256"]):
            raise RuntimeError("master file failed hash verification")

        # (4..7) Process every page.
        pages = _pipeline(job_id, master)

        # (8) Store the finished result.
        result = build_job_result(pages)
        db.set_result(job_id, "done", json.dumps(result, ensure_ascii=False))
    except Exception as exc:  # keep the server alive, record the failure
        logging.getLogger("pinkcloud.job").exception("job %s failed", job_id)
        db.set_result(job_id, "error", json.dumps({"error": str(exc)}))


def _read_multi(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    """Validate + read every image of a multi-image upload, in order.
    Same rules as the single-file path (type AND extension, then decode),
    all BEFORE any job row exists; PDFs are single-file only."""
    if len(files) > MAX_FILES_PER_JOB:
        raise HTTPException(
            status_code=400,
            detail=f"too many files: at most {MAX_FILES_PER_JOB} images per job")
    uploads: list[tuple[str, bytes]] = []
    budget = MAX_UPLOAD_BYTES  # shared by all images of the job
    for i, f in enumerate(files, start=1):
        name = f.filename or f"upload-{i}"
        ctype = (f.content_type or "").lower()
        ext = Path(name).suffix.lower()
        if (ctype not in ALLOWED_TYPES or ext not in storage.ALLOWED_EXTS
                or ext == ".pdf" or ctype == "application/pdf"):
            raise HTTPException(
                status_code=400,
                detail=f"file {i} ({name}): unsupported type for multi-image "
                       "jobs: use jpg, jpeg, png, tiff or webp")
        data = _read_capped(f, budget)
        budget -= len(data)
        try:
            probe_decode(ext, data)
        except Exception:
            raise HTTPException(
                status_code=422,
                detail=f"file {i} ({name}) could not be decoded "
                       "(corrupt or empty image)")
        uploads.append((name, data))
    return uploads


@app.post("/jobs")
def create_job(file: UploadFile | None = File(None),
               files: list[UploadFile] | None = File(None)):
    """Accept an upload, process it, return the new job id.

    Send EITHER `file` (one PDF/image, unchanged behaviour) OR a repeated
    `files` field (images, in order -> one job, one page per image).

    Validation happens in order, BEFORE any job row is created:
      1. content type AND extension both supported -> else 400
      2. bytes must actually decode (PDF opens / image decodes) -> else 422
    """
    if file is not None and files:
        raise HTTPException(
            status_code=400, detail="send either file or files, not both")
    if files:
        uploads = _read_multi(files)
        digest = storage.combined_sha256(
            [storage.sha256_bytes(d) for _, d in uploads])
        job_id = uuid.uuid4().hex
        db.create_job(job_id, uploads[0][0], digest)
        _run_multi_job(job_id, uploads, digest)
        return {"job_id": job_id}
    if file is None:
        raise HTTPException(status_code=422, detail="no file uploaded")

    filename = file.filename or "upload"
    ctype = (file.content_type or "").lower()
    ext = Path(filename).suffix.lower()

    # BOTH checks must pass (the old code accepted either one, which let
    # evil.exe ride in with a spoofed image/png content type).
    if ctype not in ALLOWED_TYPES or ext not in storage.ALLOWED_EXTS:
        raise HTTPException(
            status_code=400,
            detail="unsupported file type: use pdf, jpg, jpeg, png, tiff or webp",
        )

    data = _read_capped(file, MAX_UPLOAD_BYTES)

    # Corrupt uploads are rejected here — not stored as jobs that fail later.
    try:
        page_count = probe_decode(ext, data)
    except Exception:
        raise HTTPException(
            status_code=422,
            detail="file could not be decoded (corrupt or empty document)",
        )
    if ext == ".pdf" and page_count > MAX_PDF_PAGES:
        raise HTTPException(
            status_code=413,
            detail=f"PDF has {page_count} pages: at most {MAX_PDF_PAGES} pages per job",
        )

    job_id = uuid.uuid4().hex  # also the folder name under uploads/
    db.create_job(job_id, filename, storage.sha256_bytes(data))

    # Hackathon-pragmatic: process synchronously so POST returns when the
    # result is ready. Swap _run_job for a background task later if needed.
    _run_job(job_id, filename, data)

    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    """Return status + result JSON for one job.

    Every line carries confidence plus a per-line needs_review flag (the
    receipt's human-review rule). enrich_page() back-fills the flag on jobs
    stored before it existed, so old and new jobs answer in the same shape;
    suggestions[] passes through untouched whenever the repair module put it
    in the stored JSON."""
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    result = parse_job_result(job["result_json"])
    if result and isinstance(result.get("pages"), list):
        result["pages"] = [enrich_page(p) for p in result["pages"]]
    return {
        "job_id": job["id"],
        "filename": job["filename"],
        "sha256": job["sha256"],
        "status": job["status"],
        "created_at": job["created_at"],
        "result": result,
    }


def _job_summary(job: dict) -> dict:
    """List-row shape for GET /jobs: everything a jobs UI needs without
    fetching each job's full result. page_count / pages_needing_review /
    receipt_url are null until the job is done; error carries the failure
    message for failed jobs."""
    result = parse_job_result(job["result_json"])
    done = job["status"] == "done"
    pages = (result or {}).get("pages") or [] if done else []
    return {
        "job_id": job["id"],
        "filename": job["filename"],
        "sha256": job["sha256"],
        "status": job["status"],
        "created_at": job["created_at"],
        "page_count": len(pages) if done else None,
        "pages_needing_review": (
            sum(1 for p in pages if p.get("needs_review")) if done else None
        ),
        # Saved reviewer fixes that still apply to the current OCR text
        # (stale ones whose before-word no longer matches are not counted).
        "corrections_count": (
            _export.count_applicable_corrections(job["id"], pages)
            if done else None
        ),
        "error": (result or {}).get("error") if job["status"] == "error" else None,
        "result_url": f"/jobs/{job['id']}",
        "receipt_url": f"/jobs/{job['id']}/receipt" if done else None,
    }


@app.get("/jobs")
def list_jobs(limit: int = Query(50, ge=1, le=200),
              offset: int = Query(0, ge=0)):
    """List jobs, newest first, with the fields a jobs UI needs.
    Paginate with limit/offset; total is the full job count."""
    rows, total = db.list_jobs(limit, offset)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "jobs": [_job_summary(r) for r in rows],
    }


@app.get("/search")
def search(q: str = Query(..., min_length=1, max_length=200),
           limit: int = Query(20, ge=1, le=100),
           offset: int = Query(0, ge=0)):
    """Full-text search over the OCR text of finished jobs.

    q is matched as whole-word tokens (implicit AND). Results carry the
    job + page + line refs and a snippet with hits wrapped in <mark>.
    Only 'done' jobs are searched. The index holds the FINAL line text:
    saved reviewer corrections are folded in when the job finishes and the
    index is refreshed on every corrections PUT, so searching a corrected
    word hits the corrected page."""
    if not q.strip():
        raise HTTPException(status_code=400, detail="empty search query")
    if not db.fts_available():
        raise HTTPException(status_code=503,
                            detail="search index unavailable in this build")
    rows, total = db.search_lines(q.strip(), limit, offset)
    return {
        "query": q.strip(),
        "total": total,
        "limit": limit,
        "offset": offset,
        "results": [
            {
                "job_id": r["job_id"],
                "filename": r["filename"],
                "page": r["page"],
                "line": r["line"],
                "snippet": r["snippet"],
                "score": round(float(r["score"]), 4),
            }
            for r in rows
        ],
    }

# Job ids are uuid4 hex (32 lowercase hex chars); anything else was never
# created by POST /jobs, so it is a 404, not a 422. Page numbers are plain
# decimal digits - "0", "-1", "1/..", "abc" all miss the route's contract
# and answer 404 as well.
_JOB_ID_RE = re.compile(r"[0-9a-f]{32}")
_PAGE_NO_RE = re.compile(r"[0-9]+")


# One page render at a time: pypdfium2 is not thread-safe (concurrent renders
# crash the whole server) and each full render of a PDF costs ~150 MB, so a
# burst of page requests could also OOM-kill the process. Shared with
# pdfutil/export when they define it.
from . import pdfutil as _pdfutil  # noqa: E402
import threading as _threading  # noqa: E402

_PAGE_RENDER_LOCK = getattr(_pdfutil, "PDFIUM_LOCK", None)
if _PAGE_RENDER_LOCK is None:
    _PAGE_RENDER_LOCK = _pdfutil.PDFIUM_LOCK = _threading.RLock()


def _rendered_page_png(job_id: str, page_number: int) -> Path | None:
    """Render one page of the job's master to PNG, cached on disk.

    Uses the exact same loader as the pipeline (pdfutil.load_pages:
    pypdfium2 at ~200 DPI for PDFs, cv2 for images, ALL pages of TIFFs,
    long side capped at 1600 px) so the PNG lives in the same coordinate
    space the editor draws bboxes in. Masters are immutable per job, so a
    cached page never goes stale. The first miss renders the master once
    and caches EVERY page, so later page requests are plain file reads.
    Returns None when the page cannot be rendered.
    """
    cache_path = storage.UPLOAD_ROOT / job_id / f"page-{page_number}.png"
    if cache_path.is_file():
        return cache_path

    with _PAGE_RENDER_LOCK:
        if cache_path.is_file():  # another request rendered it meanwhile
            return cache_path
        masters = storage.master_paths(job_id)
        if not masters:
            return None
        try:
            pages = load_pages(masters)
        except Exception:  # master unreadable -> treat as missing page
            return None
        if not 1 <= page_number <= len(pages):
            return None

        for no, img in enumerate(pages, start=1):
            target = storage.UPLOAD_ROOT / job_id / f"page-{no}.png"
            if target.is_file():
                continue
            # Write-then-rename so a crash mid-write never leaves a corrupt
            # cache. (cv2.imwrite picks its encoder from the extension, so
            # the temp name must still end in .png.)
            tmp_path = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp.png")
            if cv2.imwrite(str(tmp_path), img):
                tmp_path.replace(target)
            else:
                tmp_path.unlink(missing_ok=True)
    return cache_path if cache_path.is_file() else None


@app.get("/jobs/{job_id}/pages/{n}/image")
def get_job_page_image(job_id: str, n: str):
    """Serve page `n` (1-based) of a finished job as a PNG, rendered the
    same way the pipeline renders it. Unknown/unfinished jobs and
    out-of-range pages answer 404."""
    if not _JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = db.get_job(job_id)
    if job is None or job["status"] != "done":
        raise HTTPException(status_code=404, detail="job not found")

    if not _PAGE_NO_RE.fullmatch(n) or int(n) < 1:
        raise HTTPException(status_code=404, detail="page not found")
    page_number = int(n)

    result = parse_job_result(job["result_json"])
    page_count = len((result or {}).get("pages", []))
    if page_number > page_count:
        raise HTTPException(status_code=404, detail="page not found")

    png = _rendered_page_png(job_id, page_number)
    if png is None:
        raise HTTPException(status_code=404, detail="page not found")
    return FileResponse(png, media_type="image/png")


# --- corrections: reviewer fixes persisted per job -----------------------
# Contract: schema/corrections spec (editor at main 62843be builds on it).
# PUT replaces the job's whole correction map; GET returns it. Stored as one
# JSON document at uploads/<job_id>/corrections.json - the name never
# matches the master.* glob, so master hashing/verification is unaffected.
#
# 404 ONLY for an unknown job: the editor reads any 404 from this route as
# "endpoint not deployed" and silently falls back to localStorage.
import os
import re as _re
import tempfile
from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

CORRECTIONS_FILE = "corrections.json"
MAX_CORRECTIONS = 10_000  # soft cap per job; bounds the JSON blob
_CORR_JOB_ID_RE = _re.compile(r"[0-9a-f]{32}")  # same rule as /jobs/{id}/image


class Correction(BaseModel):
    # 1-based, matches result.pages[].page
    page: int = Field(ge=1)
    # line id, e.g. "L3"
    line: str = Field(min_length=1, max_length=32)
    # 1-based word index in the line body (split on whitespace)
    word: int = Field(ge=1)
    # OCR word as recognized
    before: str = Field(max_length=256)
    # reviewer-accepted replacement; stripped, and blank-after-strip is a 422
    after: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=256)]


class CorrectionsDoc(BaseModel):
    corrections: list[Correction] = Field(max_length=MAX_CORRECTIONS)


# One lock per job serializes PUTs for that job (single-process server).
_corr_locks: dict[str, _threading.Lock] = {}
_corr_locks_guard = _threading.Lock()


def _corrections_lock(job_id: str) -> _threading.Lock:
    with _corr_locks_guard:
        return _corr_locks.setdefault(job_id, _threading.Lock())


def _require_job(job_id: str) -> None:
    if not _CORR_JOB_ID_RE.fullmatch(job_id) or db.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")


def _corrections_path(job_id: str) -> Path:
    # Read storage.UPLOAD_ROOT at call time (tests redirect it).
    return storage.UPLOAD_ROOT / job_id / CORRECTIONS_FILE


@app.get("/jobs/{job_id}/corrections")
def get_corrections(job_id: str):
    """Return the job's saved corrections ([] / null when none yet)."""
    _require_job(job_id)
    path = _corrections_path(job_id)
    if not path.is_file():
        return {"job_id": job_id, "corrections": [], "updated_at": None}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Never 404 here - that would hide a real failure behind the
        # editor's "not deployed" fallback.
        logging.getLogger("pinkcloud.corrections").exception(
            "unreadable corrections for job %s", job_id)
        raise HTTPException(status_code=500, detail="corrections unreadable")
    if not isinstance(doc, dict):  # valid JSON, but not the object we write
        logging.getLogger("pinkcloud.corrections").error(
            "corrections for job %s are not a JSON object", job_id)
        raise HTTPException(status_code=500, detail="corrections unreadable")
    return {
        "job_id": job_id,
        "corrections": doc.get("corrections", []),
        "updated_at": doc.get("updated_at"),
    }


@app.put("/jobs/{job_id}/corrections")
def put_corrections(job_id: str, body: CorrectionsDoc):
    """Replace the job's full correction map. An empty array clears it."""
    _require_job(job_id)
    path = _corrections_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    doc = {
        "corrections": [c.model_dump() for c in body.corrections],
        "updated_at": updated_at,
    }
    # Write-then-rename so a crash never leaves a half-written file. Each
    # write gets its own temp file (concurrent PUTs used to share one name
    # and collide), and a per-job lock keeps last-writer-wins ordering sane.
    with _corrections_lock(job_id):
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent,
                prefix=CORRECTIONS_FILE + ".", suffix=".tmp",
                delete=False) as fh:
            tmp_name = fh.name
            try:
                fh.write(json.dumps(doc, ensure_ascii=False))
            except BaseException:
                fh.close()
                os.unlink(tmp_name)
                raise
        try:
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        # Corrections are the S2 save path: the FTS index holds FINAL
        # (corrected) line text, so refresh this job's rows with the new
        # map. A no-op for unfinished jobs and FTS-less builds.
        db.reindex_job(job_id)
    return {"job_id": job_id, **doc}


# --------------------------------------------------------------------------
# Export + receipt (slice: pipe-export-receipt) — logic lives in export.py
# --------------------------------------------------------------------------

from fastapi.responses import PlainTextResponse, Response  # noqa: E402

from urllib.parse import quote as _quote  # noqa: E402

from . import export as _export  # noqa: E402


def _done_job(job_id: str) -> tuple[dict, list[dict]]:
    """404 unknown job, 409 unfinished/failed job, else (job, pages)."""
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job["status"] != "done":
        raise HTTPException(status_code=409, detail=f"job is {job['status']}, not done")
    result = parse_job_result(job["result_json"]) or {}
    # Reviewer corrections saved for this job (uploads/<id>/corrections.json)
    # are applied to every export and counted in the receipt.
    return job, _export.apply_saved_corrections(job_id, result.get("pages") or [])


def _receipt(job: dict, pages: list[dict], reviewer: str | None) -> dict:
    return _export.build_receipt(
        job, pages, reviewer=reviewer,
        ocr_engine=engine_status().get("ocr_engine"),
    )


def _download_name(job: dict, ext: str) -> str:
    """ASCII-only download name. HTTP headers are latin-1, so a Tamil
    filename here used to crash export.txt/export.pdf with a 500."""
    stem = Path(job["filename"]).stem
    safe = "".join(c if c.isascii() and (c.isalnum() or c in "-_") else "_"
                   for c in stem).strip("_")
    return f"{safe or 'pinkcloud'}.{ext}"


def _content_disposition(job: dict, ext: str) -> str:
    """attachment; ASCII filename= fallback + RFC 5987 filename*= that keeps
    the original (e.g. Tamil) name for browsers that support it."""
    stem = Path(job["filename"]).stem or "pinkcloud"
    # Keep the real name (Tamil vowel signs are not isalnum); only drop
    # path/quote/control chars. quote() makes the rest header-safe.
    uni = "".join(c if c.isprintable() and c not in '/\\"' else "_"
                  for c in stem)
    return (f'attachment; filename="{_download_name(job, ext)}"; '
            f"filename*=UTF-8''{_quote(f'{uni}.{ext}', safe='')}")


@app.get("/jobs/{job_id}/receipt")
def get_receipt(job_id: str, reviewer: str | None = None):
    """Processing receipt: page count, auto vs human-review counts,
    corrections, reviewer, times, master SHA-256 (re-verified on disk)."""
    job, pages = _done_job(job_id)
    return _receipt(job, pages, reviewer)


@app.get("/jobs/{job_id}/export.txt")
def export_txt(job_id: str, reviewer: str | None = None):
    """Plain UTF-8 text with a '# ' provenance header, page by page."""
    job, pages = _done_job(job_id)
    body = _export.build_txt(pages, _receipt(job, pages, reviewer))
    return PlainTextResponse(
        body, media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": _content_disposition(job, "txt"),
            "X-Master-SHA256": job["sha256"],
        },
    )


@app.get("/jobs/{job_id}/export.pdf")
def export_pdf(job_id: str, reviewer: str | None = None, receipt_page: bool = True):
    """Searchable PDF: scan image + invisible Tamil text layer per line,
    a visible receipt page at the end, provenance in the PDF Info dict."""
    job, pages = _done_job(job_id)
    masters = storage.master_paths(job_id)
    if not masters:
        raise HTTPException(status_code=410, detail="master file missing")
    receipt = _receipt(job, pages, reviewer)
    data = _export.build_pdf(masters, pages, receipt, receipt_page=receipt_page)
    return Response(
        data, media_type="application/pdf",
        headers={
            "Content-Disposition": _content_disposition(job, "pdf"),
            "X-Master-SHA256": job["sha256"],
        },
    )


DOCX_MEDIA_TYPE = ("application/vnd.openxmlformats-officedocument."
                   "wordprocessingml.document")


@app.get("/jobs/{job_id}/export.docx")
def export_docx(job_id: str, reviewer: str | None = None):
    """Word document: same corrections-applied text as export.txt, one
    heading per page, receipt as the final section."""
    job, pages = _done_job(job_id)
    data = _export.build_docx(pages, _receipt(job, pages, reviewer))
    return Response(
        data, media_type=DOCX_MEDIA_TYPE,
        headers={
            "Content-Disposition": _content_disposition(job, "docx"),
            "X-Master-SHA256": job["sha256"],
        },
    )

# --- demo box: serve the UI from the API origin (no CORS needed) ---------
# Whitelist only: never expose pinkcloud.db or uploads/ over HTTP.
#
# ORDERING REQUIREMENT: this block MUST stay the last thing in main.py.
# The /{name} and /{subdir}/{name} catch-alls below have to come AFTER
# every API route, including the export routes currently living on the
# unmerged branch slice/pipe-export-receipt — when that branch merges,
# its routes must be inserted ABOVE this block, or the catch-alls will
# swallow them.
import re

from fastapi.responses import FileResponse

UI_ROOT = Path(__file__).resolve().parents[3]

# Root-level UI assets the pages load.
UI_FILES = {
    "index.html", "editor.html", "export.html", "library.html",
    "api.js", "upload.js", "editor.js", "translit.js", "export.js", "library.js", "nav-back.js", "intro.js",
    "tokens.css", "ui.css", "upload.css", "editor.css", "export.css", "library.css", "intro.css", "sidebar.css",
    "demo.mp4",  # watch-demo modal on index.html + editor.html
}

# Sub-path assets the UI actually references. Whitelisted per directory —
# the schema/ and fonts/ directories themselves are never mounted, so
# schema/schema.json, schema/endpoints.md and the font license file stay
# unreachable even though they sit next to the served files.
UI_SUB_FILES = {
    "schema": {"doc_demo.json"},
    "samples": {"sample-page.png"},  # upload.js "Try a sample page"
    "fonts": {
        "satoshi-regular.woff2",
        "satoshi-medium.woff2",
        "satoshi-bold.woff2",
        "noto-sans-tamil.ttf",
    },
}

# Job ids are uuid4 hex (see create_job); anything else is not a job.
_JOB_ID_RE = re.compile(r"[0-9a-f]{32}")


@app.get("/", include_in_schema=False)
def ui_index():
    return FileResponse(UI_ROOT / "index.html")


@app.get("/jobs/{job_id}/image")
def job_master_image(job_id: str):
    """Serve the stored master scan for a job (read-only).

    The job_id must be a real uuid4-hex id of an existing job, so path
    traversal is impossible; uploads/ itself is never mounted or listed.
    """
    if not _JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    if db.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    master = storage.master_path(job_id)
    if master is None:
        raise HTTPException(status_code=404, detail="no master stored for job")
    return FileResponse(master)


@app.get("/{subdir}/{name}", include_in_schema=False)
def ui_sub_file(subdir: str, name: str):
    allowed = UI_SUB_FILES.get(subdir)
    if allowed is None or name not in allowed:
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(UI_ROOT / subdir / name)


@app.get("/{name}", include_in_schema=False)
def ui_file(name: str):
    if name not in UI_FILES:
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(UI_ROOT / name)

