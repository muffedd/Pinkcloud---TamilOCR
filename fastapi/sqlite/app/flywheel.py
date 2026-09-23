"""Corrections flywheel: a cross-job dictionary of reviewer-accepted fixes.

Every corrections PUT is diffed against the job's previous map; newly
accepted (before -> after) pairs are folded in here:

  - corrections_dict: one row per before-word with the accepted
    replacement and accept/skip tallies. A pair becomes a suggestion
    (GET /dictionary) once accepts >= 2 and accepts > skips - a single
    reviewer, one job, or a fix reviewers keep undoing never leaks in.
  - raw_corrections: the append-only log of every accepted pair with
    job/page/line context. This is the future fine-tuning dataset; it is
    never read back by the API.

Storage: Turso (libsql embedded replica) when LIBSQL_URL and
LIBSQL_AUTH_TOKEN are both set AND the libsql package is installed,
otherwise a plain local SQLite file (flywheel.db, next to pinkcloud.db).
Zero config = local file, so dev, tests and the demo run unchanged; set
the two env vars on Render to share one dictionary across deploys.
"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Optional dependency: the app must still run when libsql is not
# installed (requirements.txt lists it, but a minimal local venv or an
# older deploy may not have it).
try:
    import libsql as _libsql
except ImportError:  # pragma: no cover - depends on the environment
    _libsql = None

# Lives next to pinkcloud.db; tests redirect this to a tmp path.
DB_PATH = Path(__file__).resolve().parent.parent / "flywheel.db"

# A pair is served as a suggestion once it was accepted at least this
# many times AND more often accepted than skipped.
MIN_ACCEPTS = 2


def using_libsql() -> bool:
    """True when the Turso embedded-replica path is active (package +
    both env vars present). False = plain local sqlite3 fallback."""
    return (_libsql is not None
            and bool(os.environ.get("LIBSQL_URL"))
            and bool(os.environ.get("LIBSQL_AUTH_TOKEN")))


def _connect():
    """Open a connection in whichever mode is configured."""
    if using_libsql():
        conn = _libsql.connect(
            str(DB_PATH),
            sync_url=os.environ["LIBSQL_URL"],
            auth_token=os.environ["LIBSQL_AUTH_TOKEN"],
        )
    else:
        conn = sqlite3.connect(DB_PATH)
    return conn


def _init(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS corrections_dict (
            before     TEXT PRIMARY KEY,  -- OCR word as recognized
            after      TEXT NOT NULL,     -- reviewer-accepted replacement
            accepts    INTEGER NOT NULL DEFAULT 0,
            skips      INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL      -- ISO timestamp (UTC)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_corrections (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            before     TEXT NOT NULL,
            after      TEXT NOT NULL,
            job_id     TEXT,              -- job that produced the fix
            page       INTEGER,           -- 1-based page
            line       TEXT,              -- line id, e.g. 'L3'
            created_at TEXT NOT NULL      -- ISO timestamp (UTC)
        )
        """
    )


def _sync(conn) -> None:
    """Push/pull the embedded replica after a write (libsql only; a sync
    failure must never break the request, the local file is durable)."""
    sync = getattr(conn, "sync", None)
    if callable(sync):
        try:
            sync()
        except Exception:  # pragma: no cover - needs a live Turso backend
            import logging
            logging.getLogger("pinkcloud.flywheel").exception(
                "flywheel replica sync failed (writes are safe locally)")


def init_db() -> None:
    """Create the tables if they don't exist yet. Safe to call often."""
    conn = _connect()
    try:
        _init(conn)
        conn.commit()
        _sync(conn)
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def put(before: str, after: str,
        job_id: str | None = None, page: int | None = None,
        line: str | None = None) -> None:
    """Record one accepted correction pair: upsert the dictionary row
    (accepts + 1, `after` tracks the latest accepted replacement) and
    append the raw log row used for future fine-tuning."""
    now = _now()
    conn = _connect()
    try:
        _init(conn)
        conn.execute(
            "INSERT INTO corrections_dict (before, after, accepts, skips, updated_at)"
            " VALUES (?, ?, 1, 0, ?)"
            " ON CONFLICT(before) DO UPDATE SET"
            "   accepts = accepts + 1,"
            "   after = excluded.after,"
            "   updated_at = excluded.updated_at",
            (before, after, now),
        )
        conn.execute(
            "INSERT INTO raw_corrections (before, after, job_id, page, line, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (before, after, job_id, page, line, now),
        )
        conn.commit()
        _sync(conn)
    finally:
        conn.close()


def skip(before: str, after: str) -> None:
    """Record that a reviewer dismissed a suggested pair (skips + 1).
    A suggestion vanishes once skips catch up with accepts. `after` is
    kept for the audit trail; the stored replacement is not rewritten
    by a skip."""
    now = _now()
    conn = _connect()
    try:
        _init(conn)
        conn.execute(
            "INSERT INTO corrections_dict (before, after, accepts, skips, updated_at)"
            " VALUES (?, ?, 0, 1, ?)"
            " ON CONFLICT(before) DO UPDATE SET"
            "   skips = skips + 1,"
            "   updated_at = excluded.updated_at",
            (before, after, now),
        )
        conn.commit()
        _sync(conn)
    finally:
        conn.close()


def dictionary(limit: int = 200) -> list[dict]:
    """Pairs worth suggesting: accepted at least MIN_ACCEPTS times and
    accepted more often than skipped, strongest first."""
    conn = _connect()
    try:
        _init(conn)
        rows = conn.execute(
            "SELECT before, after, accepts FROM corrections_dict"
            " WHERE accepts >= ? AND accepts > skips"
            " ORDER BY accepts DESC, updated_at DESC"
            " LIMIT ?",
            (MIN_ACCEPTS, limit),
        ).fetchall()
        return [{"before": r[0], "after": r[1], "count": r[2]} for r in rows]
    finally:
        conn.close()


def learn_from_put(job_id: str, old: list[dict], new: list[dict]) -> int:
    """Fold a corrections-PUT into the dictionary.

    The PUT route replaces the job's whole map, so learning = the diff:
    only (before, after) pairs that were NOT in the previous map count as
    newly accepted. Re-saving the same map (or edits that only reorder /
    move a word) teaches nothing. Returns how many pairs were learned."""
    old_pairs = {(c.get("before"), c.get("after")) for c in old
                 if isinstance(c, dict)}
    learned = 0
    seen: set[tuple] = set()  # one accept per pair per PUT, even on dup rows
    for c in new:
        if not isinstance(c, dict):
            continue
        pair = (c.get("before"), c.get("after"))
        if not all(pair) or pair in old_pairs or pair in seen:
            continue
        seen.add(pair)
        put(c["before"], c["after"], job_id=job_id,
            page=c.get("page"), line=c.get("line"))
        learned += 1
    return learned
