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
