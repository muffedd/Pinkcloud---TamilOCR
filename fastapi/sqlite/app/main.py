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
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile

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

# --------------------------------------------------------------------------
# Export + receipt (slice: pipe-export-receipt) — logic lives in export.py
# --------------------------------------------------------------------------

from fastapi.responses import PlainTextResponse, Response  # noqa: E402

from . import export as _export  # noqa: E402


def _done_job(job_id: str) -> tuple[dict, list[dict]]:
    """404 unknown job, 409 unfinished/failed job, else (job, pages)."""
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job["status"] != "done":
        raise HTTPException(status_code=409, detail=f"job is {job['status']}, not done")
    result = parse_job_result(job["result_json"]) or {}
    return job, result.get("pages") or []


def _receipt(job: dict, pages: list[dict], reviewer: str | None) -> dict:
    return _export.build_receipt(
        job, pages, reviewer=reviewer,
        ocr_engine=engine_status().get("ocr_engine"),
    )


def _download_name(job: dict, ext: str) -> str:
    stem = Path(job["filename"]).stem or "pinkcloud"
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in stem)
    return f"{safe}.{ext}"


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
            "Content-Disposition": f'attachment; filename="{_download_name(job, "txt")}"',
            "X-Master-SHA256": job["sha256"],
        },
    )


@app.get("/jobs/{job_id}/export.pdf")
def export_pdf(job_id: str, reviewer: str | None = None, receipt_page: bool = True):
    """Searchable PDF: scan image + invisible Tamil text layer per line,
    a visible receipt page at the end, provenance in the PDF Info dict."""
    job, pages = _done_job(job_id)
    master = storage.master_path(job_id)
    if master is None:
        raise HTTPException(status_code=410, detail="master file missing")
    receipt = _receipt(job, pages, reviewer)
    data = _export.build_pdf(master, pages, receipt, receipt_page=receipt_page)
    return Response(
        data, media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{_download_name(job, "pdf")}"',
            "X-Master-SHA256": job["sha256"],
        },
    )
