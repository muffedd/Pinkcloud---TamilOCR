"""Master-file storage for Pink Cloud.

Rule #1 of the archive: the uploaded bytes are the master. We compute the
SHA-256 BEFORE writing anything, then write the exact bytes we received —
no re-encoding, no "helpful" image fixing at this stage.
"""

import hashlib
import re
from pathlib import Path

# Everything lives under uploads/<job_id>/ next to the project.
UPLOAD_ROOT = Path(__file__).resolve().parent.parent / "uploads"

# Only these extensions are accepted (also enforced in main.py).
ALLOWED_EXTS = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}

# Multi-image jobs keep one master per image: master-001.png, master-002.webp,
# ... in upload order. The name never matches the single-file "master.*"
# glob, so single-file jobs behave exactly as before.
_MULTI_MASTER_RE = re.compile(r"master-(\d{3,})\.[a-z0-9]+")


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


def combined_sha256(digests: list[str]) -> str:
    """Job-level hash for a multi-image job: SHA-256 over the per-image
    SHA-256 hex digests, in upload order, joined by newlines. Changing,
    dropping or reordering any image changes it."""
    return sha256_bytes("\n".join(digests).encode("ascii"))


def save_masters(job_id: str, uploads: list[tuple[str, bytes]]) -> dict:
    """Save each image of a multi-image job byte-for-byte, in order, as
    uploads/<job_id>/master-001.<ext>, master-002.<ext>, ...

    Returns {"paths": [...], "sha256s": [...], "sha256": combined}.
    """
    exts = [Path(name).suffix.lower() for name, _ in uploads]
    for ext in exts:
        if ext not in ALLOWED_EXTS:
            raise ValueError(f"unsupported extension: {ext}")

    job_dir = UPLOAD_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    paths, digests = [], []
    for i, ((_, data), ext) in enumerate(zip(uploads, exts), start=1):
        digests.append(sha256_bytes(data))  # hash FIRST, then write once
        path = job_dir / f"master-{i:03d}{ext}"
        with path.open("wb") as f:
            f.write(data)
        paths.append(path)
    return {"paths": paths, "sha256s": digests,
            "sha256": combined_sha256(digests)}


def master_paths(job_id: str) -> list[Path]:
    """All masters of a job in page order: [master.<ext>] for a single-file
    job, [master-001.<ext>, master-002.<ext>, ...] for a multi-image job."""
    job_dir = UPLOAD_ROOT / job_id
    if not job_dir.is_dir():
        return []
    single = sorted(job_dir.glob("master.*"))
    if single:
        return single[:1]
    multi = [(int(m.group(1)), p) for p in job_dir.glob("master-*")
             if (m := _MULTI_MASTER_RE.fullmatch(p.name))]
    return [p for _, p in sorted(multi)]


def verify_master(job_id: str, expected_sha256: str) -> bool:
    """Re-read the master(s) from disk and confirm the hash still matches.

    Single-file job: SHA-256 of master.<ext>. Multi-image job: the
    combined hash of every master-NNN file, in order (see combined_sha256).
    Cheap integrity check: catches truncated/corrupted/moved files.
    """
    masters = master_paths(job_id)
    if not masters:
        return False
    if masters[0].name.startswith("master."):
        return sha256_bytes(masters[0].read_bytes()) == expected_sha256
    digests = [sha256_bytes(m.read_bytes()) for m in masters]
    return combined_sha256(digests) == expected_sha256


def master_path(job_id: str) -> Path | None:
    """Locate the master file for a job (any allowed extension). For a
    multi-image job this is the first image (page 1)."""
    masters = master_paths(job_id)
    return masters[0] if masters else None
