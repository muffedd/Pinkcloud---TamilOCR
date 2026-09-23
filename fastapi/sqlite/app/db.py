"""SQLite storage for Pink Cloud jobs.

Two tables, stdlib sqlite3 only:
  - jobs: one row per uploaded document, its hash, processing status,
    and (when done) the full result JSON.
  - line_fts: FTS5 index over the FINAL line text of finished jobs (raw
    OCR with saved reviewer corrections applied), with job/page/line refs,
    powering GET /search. Rebuilt row-by-row from jobs.result_json +
    uploads/<job_id>/corrections.json, so the jobs table stays the source
    of truth. The default unicode61 tokenizer treats Tamil letters as word
    characters, so Tamil queries tokenize correctly.
"""

import json
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


def _fts5_supported(conn: sqlite3.Connection) -> bool:
    """True when this SQLite build has FTS5 (virtually all do; some
    minimal builds do not, and search then degrades to a clean 503
    instead of crashing startup)."""
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS line_fts USING fts5("
            " job_id UNINDEXED,"  # job uuid hex
            " page UNINDEXED,"    # 1-based page number
            " line UNINDEXED,"    # line id, e.g. 'L3'
            " body,"              # final line text (the indexed column)
            # unicode61 (the default, spelled out for clarity): Tamil
            # letters are word characters, so Tamil text tokenizes.
            " tokenize='unicode61'"
            ")"
        )
    except sqlite3.OperationalError:
        return False
    return True


def init_db() -> None:
    """Create the jobs table and FTS index if they don't exist yet.
    Safe to call often. Also reconciles the search index: any 'done'
    job not yet indexed (e.g. jobs from before search existed) is
    indexed here, so startup is the only migration step needed."""
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
        if not _fts5_supported(conn):
            return
        indexed = {r["job_id"] for r in conn.execute(
            "SELECT DISTINCT job_id FROM line_fts")}
        rows = conn.execute(
            "SELECT id, result_json FROM jobs WHERE status = 'done'"
        ).fetchall()
        for row in rows:
            if row["id"] not in indexed:
                _index_job_result(conn, row["id"], row["result_json"])


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


def list_jobs(limit: int, offset: int) -> tuple[list[dict], int]:
    """Newest-first page of job rows plus the total job count."""
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows], total


def _final_pages(job_id: str, result_json: str | None) -> list[dict]:
    """The job's pages with saved reviewer corrections applied - the FINAL
    line text, which is what search must find. Uses export's still-applies
    logic, so a stale correction (before-word no longer matching) changes
    nothing. Imported lazily: this module stays stdlib-only at module
    level, and export pulls in the imaging stack."""
    if not result_json:
        return []
    try:
        result = json.loads(result_json)
    except ValueError:
        return []
    from . import export
    return export.apply_saved_corrections(
        job_id, (result or {}).get("pages") or [])


def _index_job_result(conn: sqlite3.Connection, job_id: str,
                      result_json: str | None) -> None:
    """(Re)index one finished job: delete its old rows, insert one FTS row
    per FINAL (corrections-applied) line. Indexes whatever text the
    contract carries - no assumptions about the OCR engine or script."""
    conn.execute("DELETE FROM line_fts WHERE job_id = ?", (job_id,))
    for page in _final_pages(job_id, result_json):
        page_no = page.get("page")
        for line in page.get("lines") or []:
            body = (line.get("body") or "").strip()
            if not body:
                continue
            conn.execute(
                "INSERT INTO line_fts (job_id, page, line, body) "
                "VALUES (?, ?, ?, ?)",
                (job_id, page_no, line.get("id"), body),
            )


def reindex_job(job_id: str) -> None:
    """Rebuild one job's search rows from its stored result + current saved
    corrections. Called by the corrections PUT route after a save, so a
    corrected word becomes searchable immediately. No-op for unfinished
    jobs (nothing indexed) and FTS-less builds."""
    if not fts_available():
        return
    job = get_job(job_id)
    if job is None:
        return
    with _connect() as conn:
        if job["status"] == "done":
            _index_job_result(conn, job_id, job["result_json"])
        else:
            conn.execute("DELETE FROM line_fts WHERE job_id = ?", (job_id,))


def set_result(job_id: str, status: str, result_json: str) -> None:
    """Store the final status + result JSON once processing is done.
    A 'done' result is indexed for search in the same write; any other
    status leaves the job out of the index."""
    with _connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, result_json = ? WHERE id = ?",
            (status, result_json, job_id),
        )
        if _fts5_supported(conn):
            if status == "done":
                _index_job_result(conn, job_id, result_json)
            else:
                conn.execute("DELETE FROM line_fts WHERE job_id = ?", (job_id,))


def fts_available() -> bool:
    """Live check that the FTS table exists (GET /search gates on this)."""
    with _connect() as conn:
        return _fts5_supported(conn)


def _match_query(q: str) -> str:
    """Turn raw user text into a safe FTS5 MATCH string: each
    whitespace-separated token becomes a double-quoted phrase, joined
    with implicit AND. Quoting defangs FTS operators (AND/OR/NEAR/*),
    so arbitrary user input can never produce a syntax error."""
    return " ".join('"' + t.replace('"', '""') + '"' for t in q.split())


def search_lines(q: str, limit: int, offset: int) -> tuple[list[dict], int]:
    """Full-text search over indexed OCR lines, best matches first.

    Returns (rows, total). Each row: job_id, filename, page, line,
    snippet (query terms wrapped in <mark>), score (SQLite bm25; lower
    is a better match)."""
    match = _match_query(q)
    with _connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM line_fts WHERE line_fts MATCH ?",
            (match,),
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT f.job_id AS job_id, j.filename AS filename,"
            "       f.page AS page, f.line AS line,"
            "       snippet(line_fts, 3, '<mark>', '</mark>', '…', 16)"
            "           AS snippet,"
            "       bm25(line_fts) AS score"
            " FROM line_fts f JOIN jobs j ON j.id = f.job_id"
            " WHERE line_fts MATCH ?"
            " ORDER BY score"
            " LIMIT ? OFFSET ?",
            (match, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows], total
