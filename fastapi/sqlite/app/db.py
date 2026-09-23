"""SQLite storage for Pink Cloud jobs.

One tiny table, stdlib sqlite3 only. Each row = one uploaded document,
its hash, processing status, and (when done) the full result JSON.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# The database file lives next to this project, not in some system dir,
# so the whole app is portable (just delete the file to reset).
DB_PATH = Path(__file__).resolve().parent.parent / "pinkcloud.db"


def _connect() -> sqlite3.Connection:
    """Open a connection with sane settings (rows as dicts)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # lets us do dict(row)
    return conn


def init_db() -> None:
    """Create the jobs table if it doesn't exist yet. Safe to call often."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id          TEXT PRIMARY KEY,   -- uuid4 hex
                filename    TEXT NOT NULL,      -- original upload name
                sha256      TEXT NOT NULL,      -- hash of the master file
                status      TEXT NOT NULL,      -- pending | done | error
                result_json TEXT,               -- full output contract JSON
                created_at  TEXT NOT NULL       -- ISO timestamp (UTC)
            )
            """
        )


def create_job(job_id: str, filename: str, sha256: str) -> None:
    """Insert a new job row, status 'pending' until processing finishes."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO jobs (id, filename, sha256, status, result_json, created_at) "
            "VALUES (?, ?, ?, 'pending', NULL, ?)",
            (job_id, filename, sha256, now),
        )


def get_job(job_id: str) -> dict | None:
    """Return the job row as a plain dict, or None if the id is unknown."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def set_result(job_id: str, status: str, result_json: str) -> None:
    """Store the final status + result JSON once processing is done."""
    with _connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, result_json = ? WHERE id = ?",
            (status, result_json, job_id),
        )