"""Test-wide safety: the suite must never call the live Sarvam API.

A developer machine may have SARVAM_API_KEY exported for the demo. Drop it
before any test runs so end-to-end tests exercise the offline path; the
Sarvam tests set their own fake key and a mocked HTTP transport.

Background jobs: POST /jobs hands OCR to a background thread and returns
at once. Most tests check the finished job right after the POST, so by
default the job thread is joined before POST returns (the work still runs
on its own thread, exactly as in production). Tests marked
@pytest.mark.async_jobs get the real fire-and-forget behaviour.
"""

import os

import pytest

os.environ.pop("SARVAM_API_KEY", None)
# Same for the AI fix provider: no test may spend on Gemini.
for _var in ("GEMINI_API_KEY", "SUGGEST_PROVIDER", "SUGGEST_MODEL"):
    os.environ.pop(_var, None)

# Flywheel store: tests must never write into the developer's real
# flywheel.db (test pairs would show up as live dictionary suggestions in
# the demo) or, with LIBSQL_URL/LIBSQL_AUTH_TOKEN exported, into the shared
# Turso database. Drop the Turso env and point the store at a throwaway
# file before any test module imports the app. test_flywheel.py still
# redirects to its own tmp paths on top of this.
for _var in ("LIBSQL_URL", "LIBSQL_AUTH_TOKEN"):
    os.environ.pop(_var, None)

import tempfile as _tempfile  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

from app import flywheel as _flywheel  # noqa: E402

_flywheel.DB_PATH = _Path(_tempfile.mkdtemp(prefix="pc-flywheel-")) / "flywheel.db"


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "async_jobs: POST /jobs returns before the job finishes")


_ASYNC = {"on": False}


@pytest.fixture(scope="session", autouse=True)
def _join_background_jobs():
    """Session-wide, so module-scoped fixtures that POST are covered too."""
    from app import main

    real_spawn = main._spawn_job

    def spawn_maybe_join(target, *args):
        t = real_spawn(target, *args)
        if not _ASYNC["on"]:
            t.join(timeout=120)
        return t

    main._spawn_job = spawn_maybe_join
    yield
    main._spawn_job = real_spawn


@pytest.fixture(autouse=True)
def _async_jobs_marker(request):
    _ASYNC["on"] = bool(request.node.get_closest_marker("async_jobs"))
    yield
    _ASYNC["on"] = False


@pytest.fixture(autouse=True)
def _fresh_sarvam_throttle():
    """The Sarvam submission throttle is process-wide (SARVAM_RPM per 60 s);
    start every test with an empty window so tests never wait on it."""
    from app import ocr

    ocr._reset_sarvam_throttle()
    yield
    ocr._reset_sarvam_throttle()
