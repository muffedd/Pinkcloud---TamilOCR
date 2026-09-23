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
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from . import db, storage
from .ocr import engine_status, ocr_page, _get_engine
from .pdfutil import load_pages, probe_decode, to_gray
from .router import choose_profile, compute_scores
from .schema_out import build_job_result, build_page_result, parse_job_result

# Both a supported content type AND a supported extension are required;
# a mismatch on EITHER side is rejected with 400 before any job exists.
ALLOWED_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/tiff",
}

app = FastAPI(title="Pink Cloud", version="0.2.0")


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


def _pipeline(job_id: str, master: Path) -> list[dict]:
    """Process the master file -> list of per-page contract JSON objects."""
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
        ocr_lines, ocr_ms = ocr_page(img)

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


@app.post("/jobs")
async def create_job(file: UploadFile = File(...)):
    """Accept an upload, process it, return the new job id.

    Validation happens in order, BEFORE any job row is created:
      1. content type AND extension both supported -> else 400
      2. bytes must actually decode (PDF opens / image decodes) -> else 422
    """
    filename = file.filename or "upload"
    ctype = (file.content_type or "").lower()
    ext = Path(filename).suffix.lower()

    # BOTH checks must pass (the old code accepted either one, which let
    # evil.exe ride in with a spoofed image/png content type).
    if ctype not in ALLOWED_TYPES or ext not in storage.ALLOWED_EXTS:
        raise HTTPException(
            status_code=400,
            detail="unsupported file type: use pdf, jpg, jpeg, png or tiff",
        )

    data = await file.read()

    # Corrupt uploads are rejected here — not stored as jobs that fail later.
    try:
        probe_decode(ext, data)
    except Exception:
        raise HTTPException(
            status_code=422,
            detail="file could not be decoded (corrupt or empty document)",
        )

    job_id = uuid.uuid4().hex  # also the folder name under uploads/
    db.create_job(job_id, filename, storage.sha256_bytes(data))

    # Hackathon-pragmatic: process synchronously so POST returns when the
    # result is ready. Swap _run_job for a background task later if needed.
    _run_job(job_id, filename, data)

    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    """Return status + result JSON for one job."""
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    return {
        "job_id": job["id"],
        "filename": job["filename"],
        "sha256": job["sha256"],
        "status": job["status"],
        "created_at": job["created_at"],
        "result": parse_job_result(job["result_json"]),
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
        master = storage.master_path(job_id)
        if master is None:
            return None
        try:
            pages = load_pages(master)
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
from datetime import datetime, timezone

from pydantic import BaseModel, Field

CORRECTIONS_FILE = "corrections.json"
MAX_CORRECTIONS = 10_000  # soft cap per job; bounds the JSON blob
_CORR_JOB_ID_RE = _re.compile(r"[0-9a-f]{32}")  # same rule as /jobs/{id}/image


class Correction(BaseModel):
    page: int      # 1-based, matches result.pages[].page
    line: str      # line id, e.g. "L3"
    word: int      # 1-based word index in the line body (split on whitespace)
    before: str    # OCR word as recognized
    after: str     # reviewer-accepted replacement


class CorrectionsDoc(BaseModel):
    corrections: list[Correction] = Field(max_length=MAX_CORRECTIONS)


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
    # Write-then-rename so a crash never leaves a half-written file.
    tmp = path.with_name(CORRECTIONS_FILE + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return {"job_id": job_id, **doc}


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
    "index.html", "editor.html",
    "api.js", "upload.js", "editor.js", "translit.js",
    "tokens.css", "ui.css", "upload.css", "editor.css",
}

# Sub-path assets the UI actually references. Whitelisted per directory —
# the schema/ and fonts/ directories themselves are never mounted, so
# schema/schema.json, schema/endpoints.md and the font license file stay
# unreachable even though they sit next to the served files.
UI_SUB_FILES = {
    "schema": {"doc_demo.json"},
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

