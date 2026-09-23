"""Pink Cloud — Tamil OCR web app backend (pipe-skeleton-router).

Pipeline per upload:
  1. validate type        -> main.py
  2. hash + save master   -> storage.py  (byte-for-byte, SHA-256 first)
  3. create job row       -> db.py
  4. load pages           -> pdfutil.py  (PDF via pypdfium2, images via cv2)
  5. score + route        -> router.py   (FAST / HEAVY badge per page)
  6. OCR fast pass        -> ocr.py      (lazy PaddleOCR, stub if missing)
  7. build contract JSON  -> schema_out.py
  8. store result         -> db.py

Run:  uvicorn app.main:app --reload
Docs: see RUN.md
"""

import json
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile

from . import db, storage
from .ocr import ocr_page
from .pdfutil import load_pages, to_gray
from .router import choose_profile, compute_scores
from .schema_out import build_job_result, build_page_result, parse_job_result

# Only these content types are accepted (anything else -> 400).
ALLOWED_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/tiff",
}

app = FastAPI(title="Pink Cloud", version="0.1.0")


@app.on_event("startup")
def on_startup() -> None:
    """Create the SQLite table when the server boots."""
    db.init_db()


@app.get("/health")
def health():
    """Liveness check for the frontend/infra."""
    return {"ok": True}


def _pipeline(job_id: str, master: Path) -> list[dict]:
    """Process the master file -> list of per-page contract JSON objects."""
    pages_out: list[dict] = []

    # (4) Load every page as a 1600px-capped BGR image.
    for page_number, img in enumerate(load_pages(master), start=1):
        t_page = time.perf_counter()
        gray = to_gray(img)

        # (5) Quality metrics + FAST/HEAVY badge. HEAVY pages would
        #     normally go to a repair pass first — the skeleton just
        #    OCR's them directly for now.
        scores = compute_scores(gray)
        profile, scores = choose_profile(scores)

        # (6) OCR fast pass (stub lines if paddle isn't installed).
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
        db.set_result(job_id, "error", json.dumps({"error": str(exc)}))


@app.post("/jobs")
async def create_job(file: UploadFile = File(...)):
    """Accept an upload, process it, return the new job id."""
    filename = file.filename or "upload"
    ctype = (file.content_type or "").lower()
    ext_ok = Path(filename).suffix.lower() in storage.ALLOWED_EXTS
    if ctype not in ALLOWED_TYPES and not ext_ok:
        raise HTTPException(
            status_code=400,
            detail="unsupported file type: use pdf, jpg, jpeg, png or tiff",
        )

    data = await file.read()

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