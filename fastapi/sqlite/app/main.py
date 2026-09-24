"""Pink Cloud — Tamil OCR web app backend.

Pipeline per upload:
  1. validate type AND extension -> main.py   (both required; 400 before
     any job row exists; undecodable bytes -> 4xx, never a failed job)
  2. hash + save master   -> storage.py  (byte-for-byte, SHA-256 first)
  3. create job row       -> db.py
  4. load pages           -> pdfutil.py  (PDF via pypdfium2, images via
     cv2, ALL pages of multi-page TIFFs)
  5. score + route        -> router.py   (FAST / HEAVY badge per page;
     job mode light/heavy forces FAST/HEAVY on every page and skips the
     router's decision, auto keeps it)
  6. OCR fast pass        -> ocr.py      (FAST -> Gemini, HEAVY -> Sarvam,
     cross-fallback, marked stub output; engine failures are logged and
     shown in /health); extreme-aspect pages (palm leaves, long/short
     side > banding.EXTREME_ASPECT) are OCR'd in horizontal bands through
     the same route -> banding.py
  6b. text check          -> textcheck.py (per-line confidence = "text looks
     malformed" score; Sarvam's layout score kept as layout_confidence)
  7. build contract JSON  -> schema_out.py
  8. store result         -> db.py

Steps 2-8 run on a background thread (one per job, pages in order), so
POST /jobs answers with the job_id as soon as the upload is validated and
the job row exists; GET /jobs/{id} reports status "pending" (plus
progress) until the job is "done" or "error".

Run:  uvicorn app.main:app --reload
Docs: see RUN.md
"""

import contextvars
import json
import logging
import math
import os as _os
import re
import threading
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
import time
import uuid
from pathlib import Path

import cv2
import numpy as np
from fastapi import (FastAPI, File, Form, HTTPException, Query, Response,
                     UploadFile)
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse

from . import db, storage
from .banding import ocr_page_banded
from .ocr import (_route, _safe_error, engine_status, failed_page_lines,
                  job_breaker, ocr_page, page_engine, reset_page_engine,
                  warn_legacy_env)
from .pdfutil import PDFIUM_LOCK, load_pages, probe_decode, to_gray
from .preprocess import Mapper, prepare
from .router import choose_profile, compute_scores
from .schema_out import (build_job_result, build_page_result, enrich_page,
                         parse_job_result, score_ocr_lines)

# Both a supported content type AND a supported extension are required;
# a mismatch on EITHER side is rejected with 400 before any job exists.
ALLOWED_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/webp",
}

# OCR mode per job (POST /jobs form field `mode`, default auto):
#   auto  -> the router picks FAST/HEAVY per page (unchanged behaviour)
#   light -> every page on the FAST route (Gemini first, Sarvam fallback)
#   heavy -> every page on the HEAVY route (Sarvam first, Gemini fallback)
OCR_MODES = ("auto", "light", "heavy")
_FORCED_PROFILE = {"light": "FAST", "heavy": "HEAVY"}


def parse_mode(raw: str | None) -> str:
    """Normalize the upload's mode field. Missing/blank -> auto; any value
    outside OCR_MODES is rejected (422) before a job row exists, so a typo
    can never silently pick a route the user did not ask for."""
    if raw is None or not str(raw).strip():
        return db.DEFAULT_MODE
    mode = str(raw).strip().lower()
    if mode not in OCR_MODES:
        raise HTTPException(
            status_code=422,
            detail="invalid mode: use auto, light or heavy")
    return mode


def _job_mode(job: dict) -> str:
    """A stored job's mode; rows from before the column read as auto."""
    mode = job.get("mode")
    return mode if mode in OCR_MODES else db.DEFAULT_MODE


def page_profile(gray, mode: str = "auto"):
    """(profile, scores) for one page under the job's mode.

    auto: the router's FAST/HEAVY decision (choose_profile), as before.
    light/heavy: the profile is forced and choose_profile is never called.
    The 4 quality metrics are still measured in every mode - they are
    pure CV numbers the page contract requires, not a routing decision."""
    scores = compute_scores(gray)
    forced = _FORCED_PROFILE.get(mode)
    if forced is not None:
        return forced, scores
    return choose_profile(scores)


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


# Compress text responses (contract JSON, HTML/CSS/JS) over ~1 KB -
# gzip cuts their transfer by roughly 70%. Added AFTER the upload-cap
# middleware so it wraps it (add_middleware prepends): every response,
# including the 413s, can be compressed.
app.add_middleware(GZipMiddleware, minimum_size=1000)


def _read_capped(f: UploadFile, budget: int) -> bytes:
    """Read one upload, 413 if it exceeds the remaining byte budget."""
    data = f.file.read(budget + 1)
    if len(data) > budget:
        raise HTTPException(status_code=413, detail=_too_large_detail())
    return data


@app.on_event("startup")
def on_startup() -> None:
    """Create the SQLite table at boot.

    Sarvam is a hosted API read per page, so there is no engine to load;
    /health reports which keys are set. Removed PaddleOCR settings
    (SARVAM_FALLBACK=paddle, an OCR_ENGINE value other than sarvam|gemini)
    only log a warning.
    """
    logging.basicConfig(level=logging.INFO)
    db.init_db()
    try:
        _flywheel.init_db()
    except Exception:  # flywheel is additive; never block startup on it
        logging.getLogger("pinkcloud.flywheel").exception(
            "flywheel init failed; dictionary endpoints may not work")
    warn_legacy_env()


@app.get("/health")
def health():
    """Liveness check. Also reports the OCR engine state so stub output
    is never mistaken for real OCR text."""
    return {"ok": True, **engine_status(), **_suggest.status()}


# --------------------------------------------------------------------------
# background jobs
# --------------------------------------------------------------------------
# In-process progress of running jobs: job_id -> (pages_done, pages_total).
# Only a hint for GET /jobs/{id}; the DB status stays the source of truth
# (a job is finished only when its row says done/error).
_PROGRESS: dict[str, tuple[int, int | None]] = {}
_PROGRESS_LOCK = threading.Lock()


def _set_progress(job_id: str, done: int, total: int | None) -> None:
    with _PROGRESS_LOCK:
        _PROGRESS[job_id] = (done, total)


def _clear_progress(job_id: str) -> None:
    with _PROGRESS_LOCK:
        _PROGRESS.pop(job_id, None)


def job_progress(job_id: str) -> tuple[int, int | None] | None:
    with _PROGRESS_LOCK:
        return _PROGRESS.get(job_id)


def _job_error_json(exc: Exception) -> str:
    """Stored error for a failed job: one line, Sarvam key scrubbed."""
    return json.dumps({"error": _safe_error(exc)})


def _spawn_job(target, *args) -> threading.Thread:
    """Run one job's processing on its own background thread.

    One thread per job; inside it _pipeline fans pages out to OCR worker
    pools (Gemini pages in parallel, Sarvam pages behind the process-wide
    10/min submission throttle). Returns the started thread (tests join it)."""
    t = threading.Thread(target=target, args=args, daemon=True,
                         name=f"pinkcloud-job-{args[0][:8]}")
    t.start()
    return t


def _start_job(job_id: str, target, *args) -> None:
    """Hand a job to the background; if even that fails, the job row is
    marked error right away instead of staying pending forever."""
    _set_progress(job_id, 0, None)
    try:
        _spawn_job(target, job_id, *args)
    except Exception as exc:
        logging.getLogger("pinkcloud.job").exception(
            "job %s: could not start background processing", job_id)
        _clear_progress(job_id)
        db.set_result(job_id, "error", _job_error_json(exc))


def _env_workers(name: str, default: int, lo: int, hi: int) -> int:
    try:
        n = int(_os.environ.get(name) or default)
    except ValueError:
        n = default
    return max(lo, min(hi, n))


# OCR worker pools per job. Gemini pages are independent HTTP calls, so they
# run in parallel; Sarvam pages get a small lane, and the real rate limit is
# the process-wide digitise throttle in ocr._sarvam_throttle (SARVAM_RPM).
# Page number being OCR'd in the current worker context (logging / tests).
_CURRENT_PAGE: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "pinkcloud_current_page", default=None)


def current_page() -> int | None:
    """Page number the calling OCR worker is processing, else None."""
    return _CURRENT_PAGE.get()


GEMINI_WORKERS_DEFAULT = 6   # PINKCLOUD_GEMINI_WORKERS, clamped 1..8
SARVAM_WORKERS_DEFAULT = 2   # PINKCLOUD_SARVAM_WORKERS, clamped 1..4


def _ocr_one_page(job_id: str, page_number: int, img, profile: str,
                  scores, route_ms: float) -> dict:
    """Preprocess + OCR + freeze ONE page (runs on an OCR worker thread).

    reset_page_engine / ocr_page / page_engine all run on this same thread,
    so the per-thread engine record belongs to this page only."""
    t_page = time.perf_counter()
    _CURRENT_PAGE.set(page_number)  # this page's own context copy

    # (5b) Safe-wins preprocessing on a COPY (crop dark borders, deskew,
    #      grayscale background flatten, low-res upscale). The mapper puts
    #      OCR boxes back on the 1600px page, so the bbox contract is
    #      unchanged.
    try:
        prepared, mapper = prepare(img)
    except Exception:
        # Preprocessing crashing on ONE page must not sink the job
        # (same rule as OCR): fall back to the raw page, identity map.
        logging.getLogger("pinkcloud.job").exception(
            "job %s: preprocessing failed on page %d; OCR on the raw page",
            job_id, page_number)
        prepared, mapper = img, Mapper((0, 0), 0.0, 1.0, img.shape[:2])

    # (6) OCR, routed by profile: FAST -> Gemini, HEAVY -> Sarvam (the
    #     other engine is the fallback; an engine with an outage earlier in
    #     this job is skipped). A failure on ONE page that escapes
    #     ocr_page() must not sink the job: that page gets a marked stub
    #     line (-> needs_review) and the other pages still run.
    reset_page_engine()
    try:
        # Extreme-aspect pages (palm leaves) are OCR'd in horizontal bands
        # through the same ocr_page route; normal pages call it unchanged.
        ocr_lines, ocr_ms = ocr_page_banded(prepared, profile, ocr_page)
        ocr_lines = mapper.to_page(ocr_lines)
        engine, primary = page_engine()
    except Exception as exc:
        logging.getLogger("pinkcloud.job").exception(
            "job %s: OCR failed on page %d", job_id, page_number)
        ocr_lines, ocr_ms = failed_page_lines(img, exc), 0.0
        engine, primary = "stub", page_engine()[1]

    # (6b) Per-line confidence from the text check ("text looks malformed"
    #      score), not Sarvam's layout-block score (kept as
    #      layout_confidence).
    ocr_lines = score_ocr_lines(ocr_lines)

    # (7) Freeze the contract JSON for this page. processing_ms is this
    #     page's own work (routing + prep + OCR), not time spent queued.
    page_ms = route_ms + (time.perf_counter() - t_page) * 1000.0
    return build_page_result(
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
        ocr_engine=engine,
        ocr_fallback=(engine is not None and primary is not None
                      and engine != primary),
    )


def _pipeline(job_id: str, master: Path | list[Path],
              mode: str = "auto") -> list[dict]:
    """Process the master file(s) -> list of per-page contract JSON objects.
    `mode` is the job's OCR mode (see OCR_MODES / page_profile): heavy puts
    every page on the Sarvam lane, so it stays behind the Sarvam throttle.

    Route every page first (cheap quality scores), then OCR: Gemini-routed
    pages run on a thread pool, Sarvam-routed pages on a small lane behind
    the Sarvam throttle, both at once. Results come back in page order.
    One per-job circuit breaker is shared by all pages of the job."""
    # (4) Load every page as a 1600px-capped BGR image (PDF renders are
    #     serialized by PDFIUM_LOCK inside load_pages).
    pages_in = load_pages(master)
    total = len(pages_in)
    _set_progress(job_id, 0, total)
    if not total:
        return []

    # (5) Quality metrics + FAST/HEAVY badge for every page, up front
    #     (router in auto mode, forced by the job's light/heavy mode).
    routed = []  # (page_number, img, profile, scores, route_ms, lane)
    for page_number, img in enumerate(pages_in, start=1):
        t0 = time.perf_counter()
        # There is no repair pass yet: HEAVY pages get the same safe-wins
        # preprocessing as FAST pages, then go to their routed OCR engine.
        profile, scores = page_profile(to_gray(img), mode)
        route = _route(profile)
        lane = "sarvam" if route and route[0] == "sarvam" else "gemini"
        routed.append((page_number, img, profile, scores,
                       (time.perf_counter() - t0) * 1000.0, lane))

    n_gemini = sum(1 for r in routed if r[5] == "gemini")
    n_sarvam = total - n_gemini
    done_lock = threading.Lock()
    done = {"n": 0}
    results: dict[int, dict] = {}

    def page_done(page_number: int, page: dict) -> None:
        with done_lock:
            results[page_number] = page
            done["n"] += 1
            _set_progress(job_id, done["n"], total)

    def work(page_number, img, profile, scores, route_ms):
        page_done(page_number, _ocr_one_page(
            job_id, page_number, img, profile, scores, route_ms))

    tag = job_id[:8]
    pools: list[ThreadPoolExecutor] = []
    futures = []
    with job_breaker():
        try:
            lanes = {}
            if n_gemini:
                lanes["gemini"] = ThreadPoolExecutor(
                    max_workers=min(n_gemini, _env_workers(
                        "PINKCLOUD_GEMINI_WORKERS", GEMINI_WORKERS_DEFAULT, 1, 8)),
                    thread_name_prefix=f"pinkcloud-job-{tag}-gemini")
            if n_sarvam:
                lanes["sarvam"] = ThreadPoolExecutor(
                    max_workers=min(n_sarvam, _env_workers(
                        "PINKCLOUD_SARVAM_WORKERS", SARVAM_WORKERS_DEFAULT, 1, 4)),
                    thread_name_prefix=f"pinkcloud-job-{tag}-sarvam")
            pools = list(lanes.values())
            for page_number, img, profile, scores, route_ms, lane in routed:
                # Each page runs in its own copy of this thread's context so
                # it sees the job's circuit breaker (a ContextVar).
                ctx = contextvars.copy_context()
                futures.append(lanes[lane].submit(
                    ctx.run, work, page_number, img, profile, scores, route_ms))
            finished, _ = wait(futures, return_when=FIRST_EXCEPTION)
            for f in futures:
                if f in finished and f.exception() is not None:
                    raise f.exception()
        finally:
            for pool in pools:
                pool.shutdown(wait=True, cancel_futures=True)

    return [results[n] for n in sorted(results)]


def _run_multi_job(job_id: str, uploads: list[tuple[str, bytes]],
                   expected_sha256: str, mode: str = "auto") -> None:
    """Multi-image flow: one master per image, pages stacked in order."""
    try:
        saved = storage.save_masters(job_id, uploads)
        if saved["sha256"] != expected_sha256 or not storage.verify_master(
                job_id, expected_sha256):
            raise RuntimeError("master files failed hash verification")
        pages = _pipeline(job_id, saved["paths"], mode)
        result = build_job_result(pages)
        db.set_result(job_id, "done", json.dumps(result, ensure_ascii=False))
    except Exception as exc:  # keep the server alive, record the failure
        logging.getLogger("pinkcloud.job").exception("job %s failed", job_id)
        db.set_result(job_id, "error", _job_error_json(exc))
    finally:
        _clear_progress(job_id)


def _run_job(job_id: str, filename: str, data: bytes,
             mode: str = "auto") -> None:
    """The whole upload-to-result flow, wrapped so errors land in the DB."""
    try:
        # (2) Hash BEFORE writing, store master byte-for-byte.
        saved = storage.save_master(job_id, filename, data)
        master = Path(saved["path"])

        # Cheap integrity check immediately after writing.
        if not storage.verify_master(job_id, saved["sha256"]):
            raise RuntimeError("master file failed hash verification")

        # (4..7) Process every page.
        pages = _pipeline(job_id, master, mode)

        # (8) Store the finished result.
        result = build_job_result(pages)
        db.set_result(job_id, "done", json.dumps(result, ensure_ascii=False))
    except Exception as exc:  # keep the server alive, record the failure
        logging.getLogger("pinkcloud.job").exception("job %s failed", job_id)
        db.set_result(job_id, "error", _job_error_json(exc))
    finally:
        _clear_progress(job_id)


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


def _dedup_hit(digest: str, mode: str = "auto") -> dict | None:
    """Instant answer for bytes already OCR'd (upload dedup).

    When a finished job with a valid stored result exists for this exact
    content hash, POST /jobs returns that job instead of spending another
    OCR run: {"job_id": <existing>, "status": "done", "duplicate": true}.
    The client treats it like any finished job and opens the editor on the
    existing id. 'Valid' means the stored JSON parses to a non-empty page
    list with no stub (OCR-failed) page; a corrupt stored row, a stub
    result, an 'error' job or a still-pending one never matches, so
    failures always retry and in-flight uploads always run fresh.
    Only a job run in the SAME OCR mode matches (same bytes as Heavy after
    an Auto run is a fresh job)."""
    job = db.find_done_by_sha256(digest, mode)
    if job is None:
        return None
    try:
        result = parse_job_result(job["result_json"])
    except ValueError:
        return None
    pages = result.get("pages") if isinstance(result, dict) else None
    if not isinstance(pages, list) or not pages:
        return None
    # A page where both OCR engines failed carries marked [stub] lines but
    # the job still ends 'done'. Never serve that as a dedup hit: the
    # re-upload is how the user retries once the engine/key is fixed.
    if any(isinstance(pg, dict) and pg.get("ocr_engine") == "stub"
           for pg in pages):
        return None
    return {"job_id": job["id"], "status": "done", "duplicate": True,
            "mode": _job_mode(job)}


@app.post("/jobs")
def create_job(file: UploadFile | None = File(None),
               files: list[UploadFile] | None = File(None),
               mode: str | None = Form(None)):
    """Accept an upload, start processing it, return the new job id.

    Returns {"job_id": ...} (HTTP 200) as soon as the upload is validated
    and the job row exists; OCR runs on a background thread. Poll
    GET /jobs/{id} until status is "done" or "error".

    Send EITHER `file` (one PDF/image, unchanged behaviour) OR a repeated
    `files` field (images, in order -> one job, one page per image).

    Optional form field `mode`: auto (default, per-page router) | light
    (every page FAST -> Gemini) | heavy (every page HEAVY -> Sarvam). Any
    other value -> 422 before a job row exists. Stored on the job row and
    echoed as "mode" in every job response.

    Dedup: if the content hash (the file's sha256; for `files` the combined
    per-image hash) matches a finished job with a valid result, that job is
    returned at once ({"job_id", "status": "done", "duplicate": true}) and
    no OCR runs. Pending/error matches start a fresh job as before.

    Validation happens in order, BEFORE any job row is created:
      1. content type AND extension both supported -> else 400
      2. bytes must actually decode (PDF opens / image decodes) -> else 422
    """
    if file is not None and files:
        raise HTTPException(
            status_code=400, detail="send either file or files, not both")
    mode = parse_mode(mode)
    if files:
        uploads = _read_multi(files)
        digest = storage.combined_sha256(
            [storage.sha256_bytes(d) for _, d in uploads])
        hit = _dedup_hit(digest, mode)
        if hit is not None:
            return hit
        job_id = uuid.uuid4().hex
        db.create_job(job_id, uploads[0][0], digest, mode)
        _start_job(job_id, _run_multi_job, uploads, digest, mode)
        return {"job_id": job_id, "status": "pending", "mode": mode}
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

    digest = storage.sha256_bytes(data)
    hit = _dedup_hit(digest, mode)
    if hit is not None:
        return hit

    job_id = uuid.uuid4().hex  # also the folder name under uploads/
    db.create_job(job_id, filename, digest, mode)

    # OCR runs in the background; the client polls GET /jobs/{id}.
    _start_job(job_id, _run_job, filename, data, mode)

    return {"job_id": job_id, "status": "pending", "mode": mode}


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
    out = {
        "job_id": job["id"],
        "filename": job["filename"],
        "sha256": job["sha256"],
        "status": job["status"],
        "created_at": job["created_at"],
        "mode": _job_mode(job),
        "result": result,
    }
    if job["status"] == "pending":
        # Additive progress hint while the background thread works
        # (absent when this process is not running the job).
        prog = job_progress(job["id"])
        if prog is not None:
            done, total = prog
            out["pages_done"] = done
            out["pages_total"] = total
            if total:
                out["progress"] = min(99, int(done * 100 / total))
    return out


def _job_summary(job: dict) -> dict:
    """List-row shape for GET /jobs: everything a jobs UI needs without
    fetching each job's full result. page_count / pages_needing_review /
    receipt_url are null until the job is done; error carries the failure
    message for failed jobs.

    The counts come from the row's summary columns (db.set_result and the
    corrections PUT keep them current), so listing never parses the full
    result JSON or reads corrections.json. corrections_count counts saved
    reviewer fixes that still apply to the current OCR text (stale ones
    whose before-word no longer matches are not counted)."""
    done = job["status"] == "done"
    counts = {k: job.get(k) for k in db.SUMMARY_COLUMNS}
    if done and counts["page_count"] is None:
        # Row finished without counts (written outside set_result):
        # compute once and store, so later lists read the columns.
        counts = db.ensure_summary(job["id"]) or counts
    error = None
    if job["status"] == "error":
        result = parse_job_result(job.get("result_json"))
        error = (result or {}).get("error")
    return {
        "job_id": job["id"],
        "filename": job["filename"],
        "sha256": job["sha256"],
        "status": job["status"],
        "created_at": job["created_at"],
        "mode": _job_mode(job),
        "page_count": counts["page_count"] if done else None,
        "pages_needing_review": counts["pages_needing_review"] if done else None,
        "corrections_count": counts["corrections_count"] if done else None,
        "error": error,
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

# Cache policy for the demo-box static routes below:
# - fonts + demo.mp4 never change in place (a new build ships new bytes),
#   so they get a long-lived immutable entry;
# - JS/CSS are not content-hashed, so they only get a short max-age -
#   a deploy goes stale by at most a few minutes;
# - the HTML shell revalidates every load (no-cache -> cheap 304s) so it
#   never pairs a stale page with fresh JS/CSS;
# - job images are immutable per job_id (uuid4, written once) and private
#   because a master scan is user data - browser cache only, no shared cache.
_CACHE_IMMUTABLE = "public, max-age=31536000, immutable"
_CACHE_JS_CSS = "public, max-age=300"
_CACHE_HTML = "no-cache"
_CACHE_JOB = "private, max-age=31536000, immutable"


def _ui_cache_control(subdir: str, name: str) -> str | None:
    if subdir == "fonts" or name == "demo.mp4":
        return _CACHE_IMMUTABLE
    if name.endswith((".js", ".css")):
        return _CACHE_JS_CSS
    if name.endswith(".html"):
        return _CACHE_HTML
    return None  # samples/, schema/ - default heuristic caching
_PAGE_NO_RE = re.compile(r"[0-9]+")


# One page render at a time: pypdfium2 is not thread-safe (concurrent renders
# crash the whole server) and each full render of a PDF costs ~150 MB, so a
# burst of page requests could also OOM-kill the process. This is
# pdfutil's lock, shared with the pipeline and export.
_PAGE_RENDER_LOCK = PDFIUM_LOCK


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
    # One job_id renders one fixed page forever (cache file is content-derived),
    # so the browser may keep it privately and never revalidate.
    return FileResponse(png, media_type="image/png",
                        headers={"Cache-Control": _CACHE_JOB})


# --- reprocess: re-run OCR for one page, optionally rotated / cropped -------
# The editor's rotate button turns the scan view clockwise client-side and
# offers "Re-analyze": the same rotation is applied to the stored page here
# and OCR runs again, so lines/bboxes come back in the orientation the
# reviewer was looking at (palm-leaf scans are extreme-aspect and often need
# a quarter turn). The crop tool adds an optional crop rect so big empty
# borders can be cut away before OCR. The page's lines are replaced; its
# saved reviewer fixes are dropped (they pointed at the old OCR text), other
# pages keep theirs.
#
# Order and coordinate space: ROTATE FIRST, THEN CROP. `crop` is
# "x0,y0,x1,y1" as fractions (0..1) of the ROTATED page - i.e. of exactly
# what the reviewer sees in the editor viewport at that rotation - so the
# editor never needs to know the server's pixel size. Fractions are turned
# into whole pixels on the rotated image (floor for the start edge, ceil for
# the end edge).
#
# Base image: the page's CURRENT render (uploads/<job>/page-<n>.png), not the
# original master - edits compose (crop, then rotate, then crop again) and a
# crop rect drawn on an already-rotated/cropped render lands where it was
# drawn. `original=true` starts from the master render instead (undo every
# earlier rotate/crop for that page).

_REPROCESS_ROTATIONS = {
    0: None,
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}


_CROP_MIN_PX = 16  # smallest crop side (px) worth sending to OCR


def _parse_crop(crop: str | None):
    """"x0,y0,x1,y1" fractions -> tuple, or None when absent/blank.
    ValueError (-> 422) when malformed, out of 0..1 or empty."""
    if crop is None or not crop.strip():
        return None
    parts = crop.split(",")
    if len(parts) != 4:
        raise ValueError("crop must be x0,y0,x1,y1")
    try:
        x0, y0, x1, y1 = (float(v) for v in parts)
    except ValueError:
        raise ValueError("crop values must be numbers") from None
    if not all(math.isfinite(v) and 0.0 <= v <= 1.0 for v in (x0, y0, x1, y1)):
        raise ValueError("crop values must be fractions between 0 and 1")
    if not (x1 > x0 and y1 > y0):
        raise ValueError("crop needs x1 > x0 and y1 > y0")
    return x0, y0, x1, y1


def _crop_pixels(shape, frac):
    """Fractions of an (h, w) image -> whole-pixel (x0, y0, x1, y1), start
    edges floored and end edges ceiled so the rect never shrinks; ValueError
    when a side ends up under _CROP_MIN_PX."""
    h, w = shape[:2]
    x0 = max(0, min(w, math.floor(frac[0] * w)))
    y0 = max(0, min(h, math.floor(frac[1] * h)))
    x1 = max(0, min(w, math.ceil(frac[2] * w)))
    y1 = max(0, min(h, math.ceil(frac[3] * h)))
    if x1 - x0 < _CROP_MIN_PX or y1 - y0 < _CROP_MIN_PX:
        raise ValueError(f"crop is too small (min {_CROP_MIN_PX}px a side)")
    return x0, y0, x1, y1


def _drop_page_corrections(job_id: str, page_number: int) -> None:
    """Remove saved corrections for one page (reprocess replaced its lines).
    Same write-then-rename discipline as the corrections PUT."""
    path = _corrections_path(job_id)
    if not path.is_file():
        return
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        kept = [c for c in (doc.get("corrections") or [])
                if isinstance(c, dict) and c.get("page") != page_number]
    except (OSError, ValueError, AttributeError):
        return  # unreadable: the next PUT rewrites it; never block reprocess
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = {"corrections": kept, "updated_at": updated_at}
    with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=CORRECTIONS_FILE + ".", suffix=".tmp",
            delete=False) as fh:
        tmp_name = fh.name
        try:
            fh.write(json.dumps(out, ensure_ascii=False))
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


@app.post("/jobs/{job_id}/pages/{n}/reprocess")
def reprocess_job_page(job_id: str, n: str, rotate: int = Query(0),
                       crop: str | None = Query(None),
                       original: bool = Query(False)):
    """Re-run the pipeline for page `n` (1-based) of a finished job, with
    the page rotated `rotate` degrees clockwise first (0/90/180/270), then
    cropped to `crop` ("x0,y0,x1,y1" fractions of the ROTATED page; omit
    for no crop). The base is the page's current render (earlier
    rotate/crop edits compose); `original=true` starts from the master.
    The job's OCR mode applies exactly as at upload (light/heavy force the
    route, auto re-scores the new page). The rendered page image cache is
    replaced so the scan route serves the new page; the page's saved
    corrections are dropped.

    404 unknown job/page, 409 job not done, 422 unsupported rotation or bad
    crop (malformed, outside 0..1, empty, or under 16px a side)."""
    if not _JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    if not _PAGE_NO_RE.fullmatch(n) or int(n) < 1:
        raise HTTPException(status_code=404, detail="page not found")
    if rotate not in _REPROCESS_ROTATIONS:
        raise HTTPException(
            status_code=422,
            detail="rotate must be 0, 90, 180 or 270 (degrees clockwise)")
    try:
        crop_frac = _parse_crop(crop)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job["status"] != "done":
        raise HTTPException(status_code=409,
                            detail=f"job is {job['status']}, not done")
    page_number = int(n)

    # Serializes with corrections PUTs for this job (same lock): a reviewer
    # saving fixes mid-reprocess never has them silently dropped afterwards.
    with _corrections_lock(job_id):
        masters = storage.master_paths(job_id)
        if not masters:
            raise HTTPException(status_code=404, detail="page not found")
        with _PAGE_RENDER_LOCK:  # pypdfium2 is not thread-safe
            try:
                pages_in = load_pages(masters)
            except Exception:
                pages_in = []
        if not 1 <= page_number <= len(pages_in):
            raise HTTPException(status_code=404, detail="page not found")

        target = storage.UPLOAD_ROOT / job_id / f"page-{page_number}.png"
        img = None
        if not original and target.is_file():
            # Current render: what the editor shows (may already carry an
            # earlier rotate/crop). Unreadable -> fall back to the master.
            img = cv2.imread(str(target), cv2.IMREAD_COLOR)
        if img is None:
            img = pages_in[page_number - 1]
        code = _REPROCESS_ROTATIONS[rotate]
        if code is not None:
            img = cv2.rotate(img, code)
        if crop_frac is not None:
            try:
                x0, y0, x1, y1 = _crop_pixels(img.shape, crop_frac)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from None
            img = np.ascontiguousarray(img[y0:y1, x0:x1])

        t0 = time.perf_counter()
        profile, scores = page_profile(to_gray(img), _job_mode(job))
        route_ms = (time.perf_counter() - t0) * 1000.0
        with job_breaker():
            page = _ocr_one_page(job_id, page_number, img, profile, scores,
                                 route_ms)

        result = parse_job_result(job["result_json"]) or {}
        pages = result.get("pages") or []
        idx = next((i for i, p in enumerate(pages)
                    if isinstance(p, dict) and p.get("page") == page_number),
                   page_number - 1)
        if not 0 <= idx < len(pages):
            raise HTTPException(status_code=404, detail="page not found")
        pages[idx] = page
        _drop_page_corrections(job_id, page_number)
        db.set_result(job_id, "done",
                      json.dumps(build_job_result(pages), ensure_ascii=False))

        # Replace the rendered page image so GET .../image serves what OCR
        # just read (write-then-rename, same as the render cache).
        tmp_path = target.with_name(
            f"{target.name}.{uuid.uuid4().hex}.tmp.png")
        target.parent.mkdir(parents=True, exist_ok=True)
        if cv2.imwrite(str(tmp_path), img):
            tmp_path.replace(target)
        else:
            tmp_path.unlink(missing_ok=True)

    out = {"job_id": job_id, "page": page_number, "rotate": rotate,
           "status": "done"}
    if crop_frac is not None:
        out["crop"] = [x0, y0, x1, y1]  # pixels on the rotated page
        out["width"], out["height"] = int(img.shape[1]), int(img.shape[0])
    if original:
        out["original"] = True
    return out


# --- corrections: reviewer fixes persisted per job -----------------------
# Contract: schema/corrections spec (editor at main 62843be builds on it).
# PUT replaces the job's whole correction map; GET returns it. Stored as one
# JSON document at uploads/<job_id>/corrections.json - the name never
# matches the master.* glob, so master hashing/verification is unaffected.
#
# 404 ONLY for an unknown job: the editor reads any 404 from this route as
# "endpoint not deployed" and silently falls back to localStorage.
import os
import tempfile
from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from . import flywheel as _flywheel

CORRECTIONS_FILE = "corrections.json"
MAX_CORRECTIONS = 10_000  # soft cap per job; bounds the JSON blob
_CORR_JOB_ID_RE = _JOB_ID_RE  # same rule as /jobs/{id}/image


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
_corr_locks: dict[str, threading.Lock] = {}
_corr_locks_guard = threading.Lock()


def _corrections_lock(job_id: str) -> threading.Lock:
    with _corr_locks_guard:
        return _corr_locks.setdefault(job_id, threading.Lock())


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
        # Previous map, for the flywheel diff below. An unreadable file
        # diffs as empty (the save itself reports its own errors).
        old_corrections: list[dict] = []
        if path.is_file():
            try:
                prev = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(prev, dict) and isinstance(
                        prev.get("corrections"), list):
                    old_corrections = prev["corrections"]
            except (OSError, ValueError):
                logging.getLogger("pinkcloud.flywheel").warning(
                    "flywheel diff skipped: prior corrections for job %s "
                    "unreadable", job_id)
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
        # (corrected) line text and GET /jobs lists corrections_count from
        # the row, so refresh both from the new map (corrections applied
        # once). A no-op for unfinished jobs.
        db.refresh_corrections(job_id)
    # Flywheel fold-in: only pairs this PUT newly accepted (diff vs the
    # previous map) teach the cross-job dictionary. Learning must never
    # break the save, so failures are logged, not raised.
    try:
        _flywheel.learn_from_put(job_id, old_corrections, doc["corrections"])
    except Exception:
        logging.getLogger("pinkcloud.flywheel").exception(
            "flywheel learning failed for job %s", job_id)
    return {"job_id": job_id, **doc}


# --- dictionary: the cross-job corrections flywheel -----------------------
# Learned from corrections PUTs (see above). GET serves only pairs that
# were accepted at least MIN_ACCEPTS times and more often than skipped;
# POST /skip lets a reviewer kill a bad suggestion. Storage is Turso when
# LIBSQL_URL/LIBSQL_AUTH_TOKEN are set, else a local flywheel.db file, so
# everything below works with zero config.


@app.get("/dictionary")
def get_dictionary(limit: int = Query(200, ge=1, le=1000)):
    """Reviewer-proven fixes worth suggesting, strongest first."""
    return {"dictionary": _flywheel.dictionary(limit=limit),
            "min_accepts": _flywheel.MIN_ACCEPTS}


class DictionarySkip(BaseModel):
    before: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=256)]
    after: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=256)]


@app.post("/dictionary/skip")
def post_dictionary_skip(body: DictionarySkip):
    """Dismiss a suggested pair; it disappears once skips catch accepts."""
    _flywheel.skip(body.before, body.after)
    return {"before": body.before, "after": body.after, "skipped": True}


# --- AI fix: POST /jobs/{id}/suggest ---------------------------------------
# Candidate readings for one Doubt word, from the provider picked in
# app/suggest.py (off by default -> 503). Contract: schema/ai-fix-contract.md.

from . import suggest as _suggest  # noqa: E402

SUGGEST_NEIGHBORS = 2  # lines of context on each side of the target line


class SuggestRequest(BaseModel):
    page: int = Field(ge=1)
    line: str = Field(min_length=1, max_length=32)
    word: int = Field(ge=1)
    before: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=256)]
    # Editor sends the full line body; used only if the stored line is gone.
    context: str | None = Field(default=None, max_length=4000)


@app.post("/jobs/{job_id}/suggest")
def post_suggest(job_id: str, body: SuggestRequest):
    """Up to 3 AI readings for one word. 404 unknown job/page, 409 job not
    done, 503 when AI fix is off or the model fails (fixed message, never
    provider details). Accepting a candidate is a normal corrections PUT."""
    if not _CORR_JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    _job, pages = _done_job(job_id)
    page = next((p for p in pages if p.get("page") == body.page), None)
    if page is None:
        raise HTTPException(status_code=404, detail="page not found")
    lines = [ln for ln in (page.get("lines") or []) if isinstance(ln, dict)]
    idx = next((i for i, ln in enumerate(lines) if ln.get("id") == body.line), None)
    if idx is None:
        if not body.context:
            raise HTTPException(status_code=404, detail="line not found")
        target, before_ctx, after_ctx = body.context, [], []
    else:
        target = str(lines[idx].get("body") or body.context or "")
        before_ctx = [str(ln.get("body") or "")
                      for ln in lines[max(0, idx - SUGGEST_NEIGHBORS):idx]]
        after_ctx = [str(ln.get("body") or "")
                     for ln in lines[idx + 1:idx + 1 + SUGGEST_NEIGHBORS]]
    try:
        candidates = _suggest.suggest(body.before, target, body.word,
                                      before_ctx, after_ctx)
    except _suggest.SuggestUnavailable:
        raise HTTPException(status_code=503, detail="AI suggestions unavailable")
    return {"candidates": candidates}


# --------------------------------------------------------------------------
# Export + receipt — logic lives in export.py
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
    # Same read-time line semantics as GET /jobs/{id} (old jobs get the
    # text-check confidence/needs_review), so receipt counts match the API.
    pages = [enrich_page(p) for p in (result.get("pages") or [])]
    # Reviewer corrections saved for this job (uploads/<id>/corrections.json)
    # are applied to every export and counted in the receipt.
    return job, _export.apply_saved_corrections(job_id, pages)


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
    """Plain UTF-8 text: ONLY the transcribed (corrections-applied) lines,
    page by page. No header or receipt (that is GET /jobs/{id}/receipt);
    ?reviewer is accepted for old links and ignored."""
    job, pages = _done_job(job_id)
    body = _export.build_txt(pages)
    return PlainTextResponse(
        body, media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": _content_disposition(job, "txt"),
            "X-Master-SHA256": job["sha256"],
        },
    )


@app.get("/jobs/{job_id}/export.pdf")
def export_pdf(job_id: str, reviewer: str | None = None, receipt_page: bool = False,
               include_scans: bool = True):
    """Text-first PDF: the recognized (corrections-applied) lines as visible
    Tamil text pages, then each scan with its invisible text layer.
    ?include_scans=0 (or false) leaves the scan pages out: text pages only.
    Missing = 1, the old behaviour, so existing links are unchanged. No
    receipt page (receipt_page is accepted for old links and ignored; the
    receipt is GET /jobs/{id}/receipt). Job id + SHA-256 in the Info dict."""
    job, pages = _done_job(job_id)
    masters = storage.master_paths(job_id)
    if not masters:
        raise HTTPException(status_code=410, detail="master file missing")
    receipt = _receipt(job, pages, reviewer)
    data = _export.build_pdf(masters, pages, receipt, include_scans=include_scans)
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
    """Word document: ONLY the same corrections-applied text as export.txt
    ('Page N' heading per page on multi-page jobs). No receipt section;
    ?reviewer is accepted for old links and ignored."""
    job, pages = _done_job(job_id)
    data = _export.build_docx(pages, filename=job["filename"], sha256=job["sha256"])
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
# every API route (new routes go ABOVE this block), or the catch-alls
# will swallow them.
UI_ROOT = Path(__file__).resolve().parents[3]

# Root-level UI assets the pages load.
UI_FILES = {
    "index.html", "editor.html", "export.html", "library.html",
    "api.js", "upload.js", "editor.js", "translit.js", "export.js", "library.js", "nav-back.js", "intro.js", "loader.js",
    "tokens.css", "ui.css", "upload.css", "editor.css", "export.css", "library.css", "intro.css", "sidebar.css", "loader.css",
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
        "noto-sans-tamil-ta.woff2",  # Tamil-subset woff2 (see ui.css)
    },
}

@app.get("/", include_in_schema=False)
def ui_index():
    return FileResponse(UI_ROOT / "index.html",
                        headers={"Cache-Control": _CACHE_HTML})


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
    return FileResponse(master, headers={"Cache-Control": _CACHE_JOB})


@app.get("/{subdir}/{name}", include_in_schema=False)
def ui_sub_file(subdir: str, name: str):
    allowed = UI_SUB_FILES.get(subdir)
    if allowed is None or name not in allowed:
        raise HTTPException(status_code=404, detail="Not Found")
    cache = _ui_cache_control(subdir, name)
    return FileResponse(UI_ROOT / subdir / name,
                        headers={"Cache-Control": cache} if cache else None)


@app.head("/{subdir}/{name}", include_in_schema=False)
def ui_sub_file_head(subdir: str, name: str):
    """Headers-only presence probe for the whitelisted assets above.

    upload.js uses it to show/hide the "Try a sample page" button without
    downloading the 408KB sample PNG on every index load (the GET route
    does not answer HEAD, so it needs its own handler)."""
    allowed = UI_SUB_FILES.get(subdir)
    if allowed is None or name not in allowed:
        raise HTTPException(status_code=404, detail="Not Found")
    if not (UI_ROOT / subdir / name).is_file():
        raise HTTPException(status_code=404, detail="Not Found")
    return Response(status_code=200)


@app.get("/{name}", include_in_schema=False)
def ui_file(name: str):
    if name not in UI_FILES:
        raise HTTPException(status_code=404, detail="Not Found")
    cache = _ui_cache_control("", name)
    return FileResponse(UI_ROOT / name,
                        headers={"Cache-Control": cache} if cache else None)

