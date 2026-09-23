"""Master-file storage for Pink Cloud.

Rule #1 of the archive: the uploaded bytes are the master. We compute the
SHA-256 BEFORE writing anything, then write the exact bytes we received —
no re-encoding, no "helpful" image fixing at this stage.
"""

import hashlib
import shutil
from pathlib import Path

# Everything lives under uploads/<job_id>/ next to the project.
UPLOAD_ROOT = Path(__file__).resolve().parent.parent / "uploads"

# Only these extensions are accepted (also enforced in main.py).
ALLOWED_EXTS = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}


def sha256_bytes(data: bytes) -> str:
    """Hash raw bytes -> hex string. Called before we touch the disk."""
    return hashlib.sha256(data).hexdigest()


def save_master(job_id: str, filename: str, data: bytes) -> dict:
    """Save the upload byte-for-byte as uploads/<job_id>/master.<ext>.

    Returns {"path": ..., "sha256": ...} so main.py can record the hash.
    """
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise ValueError(f"unsupported extension: {ext}")

    # Hash FIRST, so the recorded hash describes the exact bytes we keep.
    digest = sha256_bytes(data)

    job_dir = UPLOAD_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    master_path = job_dir / f"master{ext}"

    # "wb" + write once = byte-for-byte copy. No Pillow, no re-encode.
    with master_path.open("wb") as f:
        f.write(data)

    return {"path": master_path, "sha256": digest}


def verify_master(job_id: str, expected_sha256: str) -> bool:
    """Re-read the master from disk and confirm the hash still matches.

    Cheap integrity check: catches truncated/corrupted/moved files.
    """
    job_dir = UPLOAD_ROOT / job_id
    masters = sorted(job_dir.glob("master.*")) if job_dir.is_dir() else []
    if not masters:
        return False
    actual = sha256_bytes(masters[0].read_bytes())
    return actual == expected_sha256


def master_path(job_id: str) -> Path | None:
    """Locate the master file for a job (any allowed extension)."""
    job_dir = UPLOAD_ROOT / job_id
    masters = sorted(job_dir.glob("master.*")) if job_dir.is_dir() else []
    return masters[0] if masters else None


def delete_job_files(job_id: str) -> None:
    """Remove a job's whole folder (handy during hacking/demo)."""
    job_dir = UPLOAD_ROOT / job_id
    if job_dir.is_dir():
        shutil.rmtree(job_dir, ignore_errors=True)