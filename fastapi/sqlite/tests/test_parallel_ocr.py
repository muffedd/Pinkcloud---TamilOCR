"""Parallel page OCR + per-job circuit breaker + request timeouts.

Gemini-routed pages run on a thread pool, Sarvam-routed pages on a small
lane behind the process-wide submission throttle; results come back in
page order with the right per-page engine label. An engine that times out
or 5xxs once in a job is skipped for the rest of that job. No network.

Run from fastapi/sqlite/:
    python -m pytest tests/test_parallel_ocr.py -v
"""

import threading
import time

import cv2
import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import db, main, ocr, storage

_sleep = threading.Event().wait  # real sleep, immune to time.sleep patches


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db.DB_PATH = tmp_path / "test.db"
    storage.UPLOAD_ROOT = tmp_path / "uploads"
    db.init_db()
    monkeypatch.setattr(ocr, "_LAST_ENGINE", None)
    with TestClient(main.app, raise_server_exceptions=True) as c:
        yield c


def _png(label="P") -> bytes:
    img = np.full((600, 900, 3), 235, np.uint8)
    cv2.putText(img, label, (80, 200), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (20, 20, 20), 3)
    return cv2.imencode(".png", img)[1].tobytes()


_POSTS = iter(range(1, 1_000_000))


def _post(client, n):
    # Unique pixels per call: identical bytes would hit upload dedup and
    # return the earlier job without running OCR.
    k = next(_POSTS)
    files = [("files", (f"p{i}.png", _png(f"{k}-{i}"), "image/png")) for i in range(1, n + 1)]
    r = client.post("/jobs", files=files)
    assert r.status_code == 200, r.text
    job = client.get(f"/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "done", job
    return job["result"]["pages"]


def _profiles(monkeypatch, seq):
    """choose_profile returns seq[i] for page i+1 (routing runs in order)."""
    it = iter(seq)
    monkeypatch.setattr(main, "choose_profile", lambda s: (next(it), s))


def _line(text):
    return [{"body": text, "bbox": [10, 10, 300, 30], "confidence": 0.9}]


# ---- parallelism + ordering ------------------------------------------------

def test_gemini_pages_run_concurrently(client, monkeypatch):
    _profiles(monkeypatch, ["FAST"] * 4)
    barrier = threading.Barrier(4, timeout=10)

    def gemini(img, client=None):
        barrier.wait()  # only passes if 4 pages are in flight at once
        return _line(f"page {main.current_page()}")

    monkeypatch.setattr(ocr, "_gemini_ocr", gemini)
    pages = _post(client, 4)
    assert [p["page"] for p in pages] == [1, 2, 3, 4]
    assert [p["lines"][0]["body"] for p in pages] == [f"page {i}" for i in range(1, 5)]


def test_results_keep_page_order_when_late_pages_finish_first(client, monkeypatch):
    _profiles(monkeypatch, ["FAST"] * 5)
    finished = []

    def gemini(img, client=None):
        n = main.current_page()
        _sleep(0.05 * (6 - n))  # page 1 slowest, page 5 fastest
        finished.append(n)
        return _line(f"page {n}")

    monkeypatch.setattr(ocr, "_gemini_ocr", gemini)
    pages = _post(client, 5)
    assert finished != sorted(finished)  # really finished out of order
    assert [p["page"] for p in pages] == [1, 2, 3, 4, 5]
    assert [p["lines"][0]["body"] for p in pages] == [f"page {i}" for i in range(1, 6)]


def test_mixed_routes_label_each_page_engine(client, monkeypatch):
    _profiles(monkeypatch, ["FAST", "HEAVY", "FAST", "HEAVY", "HEAVY"])

    def gemini(img, client=None):
        _sleep(0.02)
        return _line("g")

    def sarvam(img, client=None):
        if main.current_page() == 4:
            raise RuntimeError("bad page")  # not an outage: no breaker trip
        return _line("s")

    monkeypatch.setattr(ocr, "_gemini_ocr", gemini)
    monkeypatch.setattr(ocr, "_sarvam_ocr", sarvam)
    pages = _post(client, 5)
    assert [(p["ocr_engine"], p["ocr_fallback"]) for p in pages] == [
        ("gemini", False), ("sarvam", False), ("gemini", False),
        ("gemini", True), ("sarvam", False)]


def test_sarvam_lane_is_separate_from_gemini_pool(client, monkeypatch):
    monkeypatch.setenv("PINKCLOUD_SARVAM_WORKERS", "1")
    _profiles(monkeypatch, ["HEAVY", "HEAVY", "FAST"])
    threads = {}
    active = {"n": 0, "max": 0}
    lock = threading.Lock()

    def sarvam(img, client=None):
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        threads[main.current_page()] = threading.current_thread().name
        _sleep(0.05)
        with lock:
            active["n"] -= 1
        return _line("s")

    def gemini(img, client=None):
        threads[main.current_page()] = threading.current_thread().name
        return _line("g")

    monkeypatch.setattr(ocr, "_sarvam_ocr", sarvam)
    monkeypatch.setattr(ocr, "_gemini_ocr", gemini)
    _post(client, 3)
    assert active["max"] == 1  # PINKCLOUD_SARVAM_WORKERS=1: one at a time
    assert "-sarvam_" in threads[1] and "-sarvam_" in threads[2]
    assert "-gemini_" in threads[3]
    assert all(t.startswith("pinkcloud-job-") for t in threads.values())


def test_progress_reaches_total(client, monkeypatch):
    _profiles(monkeypatch, ["FAST"] * 3)
    monkeypatch.setattr(ocr, "_gemini_ocr", lambda img, client=None: _line("g"))
    seen = []
    real = main._set_progress
    monkeypatch.setattr(main, "_set_progress",
                        lambda j, d, t: (seen.append((d, t)), real(j, d, t)))
    _post(client, 3)
    seen = [x for x in seen if x[1] is not None]  # drop _start_job's (0, None)
    assert seen[0] == (0, 3) and seen[-1] == (3, 3)
    assert [d for d, _ in seen[1:]] == [1, 2, 3]


# ---- Sarvam throttle -------------------------------------------------------

def test_sarvam_throttle_caps_submissions_per_minute(monkeypatch):
    monkeypatch.setenv("SARVAM_RPM", "3")
    clock = {"t": 1000.0}
    waits = []
    monkeypatch.setattr(ocr.time, "monotonic", lambda: clock["t"])

    def fake_wait(s):
        waits.append(round(s, 3))
        clock["t"] += s

    monkeypatch.setattr(ocr, "_THROTTLE_WAIT", fake_wait)
    ocr._reset_sarvam_throttle()
    for _ in range(3):
        ocr._sarvam_throttle()
    assert waits == []           # first 3 inside the minute go straight out
    ocr._sarvam_throttle()
    assert waits == [60.0]       # the 4th waits for the oldest to age out


def test_sarvam_ocr_goes_through_throttle(monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "k")
    hits = []
    monkeypatch.setattr(ocr, "_sarvam_throttle", lambda: hits.append(1))

    def handler(request):
        return httpx.Response(400, text="nope")

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client",
                        lambda *a, **k: real(transport=httpx.MockTransport(handler)))
    with pytest.raises(ocr.SarvamError):
        ocr._sarvam_ocr(np.full((50, 50, 3), 255, np.uint8))
    assert hits == [1]


# ---- circuit breaker -------------------------------------------------------

def test_gemini_timeout_skips_gemini_for_rest_of_job(client, monkeypatch):
    monkeypatch.setenv("PINKCLOUD_GEMINI_WORKERS", "1")  # deterministic order
    _profiles(monkeypatch, ["FAST"] * 4)
    calls = []

    def gemini(img, client=None):
        calls.append(main.current_page())
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(ocr, "_gemini_ocr", gemini)
    monkeypatch.setattr(ocr, "_sarvam_ocr", lambda img, client=None: _line("s"))
    pages = _post(client, 4)
    assert len(calls) == 1
    assert all(p["ocr_engine"] == "sarvam" and p["ocr_fallback"] for p in pages)


def test_sarvam_5xx_skips_sarvam_for_rest_of_job(client, monkeypatch):
    monkeypatch.setenv("PINKCLOUD_SARVAM_WORKERS", "1")
    _profiles(monkeypatch, ["HEAVY"] * 3)
    calls = []

    def sarvam(img, client=None):
        calls.append(1)
        raise ocr.SarvamError("HTTP 502", outage=True)

    monkeypatch.setattr(ocr, "_sarvam_ocr", sarvam)
    monkeypatch.setattr(ocr, "_gemini_ocr", lambda img, client=None: _line("g"))
    pages = _post(client, 3)
    assert len(calls) == 1
    assert [p["ocr_engine"] for p in pages] == ["gemini"] * 3


def test_both_engines_down_all_later_pages_stub_without_calls(client, monkeypatch):
    monkeypatch.setenv("PINKCLOUD_GEMINI_WORKERS", "1")
    _profiles(monkeypatch, ["FAST"] * 3)
    calls = []

    def down(name):
        def fn(img, client=None):
            calls.append(name)
            raise httpx.ConnectTimeout("down")
        return fn

    monkeypatch.setattr(ocr, "_gemini_ocr", down("gemini"))
    monkeypatch.setattr(ocr, "_sarvam_ocr", down("sarvam"))
    pages = _post(client, 3)
    assert calls == ["gemini", "sarvam"]
    assert [p["ocr_engine"] for p in pages] == ["stub"] * 3
    assert all(p["needs_review"] for p in pages)


def test_non_outage_failure_does_not_trip_breaker(client, monkeypatch):
    monkeypatch.setenv("PINKCLOUD_GEMINI_WORKERS", "1")
    _profiles(monkeypatch, ["FAST"] * 3)
    calls = []

    def gemini(img, client=None):
        calls.append(1)
        raise ocr.GeminiError("HTTP 400", outage=False)

    monkeypatch.setattr(ocr, "_gemini_ocr", gemini)
    monkeypatch.setattr(ocr, "_sarvam_ocr", lambda img, client=None: _line("s"))
    _post(client, 3)
    assert len(calls) == 3


def test_breaker_is_per_job(client, monkeypatch):
    monkeypatch.setenv("PINKCLOUD_GEMINI_WORKERS", "1")
    _profiles(monkeypatch, ["FAST"] * 4)
    calls = []

    def gemini(img, client=None):
        calls.append(1)
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(ocr, "_gemini_ocr", gemini)
    monkeypatch.setattr(ocr, "_sarvam_ocr", lambda img, client=None: _line("s"))
    _post(client, 2)
    _post(client, 2)
    assert len(calls) == 2  # one try per job, the next job starts fresh


def test_no_breaker_outside_a_job(monkeypatch):
    calls = []

    def gemini(img, client=None):
        calls.append(1)
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(ocr, "_gemini_ocr", gemini)
    monkeypatch.setattr(ocr, "_sarvam_ocr", lambda img, client=None: _line("s"))
    img = np.full((50, 50, 3), 255, np.uint8)
    ocr.ocr_page(img, "FAST")
    ocr.ocr_page(img, "FAST")
    assert len(calls) == 2


def test_outage_classification():
    assert ocr._is_outage(httpx.ReadTimeout("t"))
    assert ocr._is_outage(httpx.ConnectError("c"))
    assert ocr._is_outage(ocr.SarvamError("x", outage=True))
    assert ocr._is_outage(ocr.GeminiError("x", outage=True))
    assert not ocr._is_outage(ocr.SarvamError("HTTP 400"))
    assert not ocr._is_outage(ocr.GeminiError("no text"))
    assert not ocr._is_outage(RuntimeError("bad page"))


# ---- Gemini HTTP: 5xx flag + 30 s default timeout ---------------------------

def _gemini_client(monkeypatch, handler, seen=None):
    real = httpx.Client

    def make(*a, **k):
        if seen is not None:
            seen.append(k.get("timeout"))
        return real(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "Client", make)


@pytest.mark.parametrize("status,outage", [(500, True), (503, True), (429, False), (400, False)])
def test_gemini_http_errors_flag_outage(monkeypatch, status, outage):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    _gemini_client(monkeypatch, lambda r: httpx.Response(status, text="err"))
    with pytest.raises(ocr.GeminiError) as ei:
        ocr._gemini_ocr(np.full((50, 50, 3), 255, np.uint8))
    assert ei.value.outage is outage


def test_gemini_default_timeout_is_30s(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.delenv("GEMINI_TIMEOUT_S", raising=False)
    seen = []
    _gemini_client(monkeypatch, lambda r: httpx.Response(500), seen)
    with pytest.raises(ocr.GeminiError):
        ocr._gemini_ocr(np.full((50, 50, 3), 255, np.uint8))
    assert seen == [30.0]
    monkeypatch.setenv("GEMINI_TIMEOUT_S", "12")
    with pytest.raises(ocr.GeminiError):
        ocr._gemini_ocr(np.full((50, 50, 3), 255, np.uint8))
    assert seen[-1] == 12.0


# ---- Sarvam: deadline on every HTTP call -----------------------------------

def test_sarvam_each_call_timeout_is_time_left():
    timeouts = []

    def handler(request):
        timeouts.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json={})

    c = httpx.Client(transport=httpx.MockTransport(handler))
    ocr._sarvam_request(c, "GET", "https://x.example/a", time.monotonic() + 5.0)
    ocr._sarvam_request(c, "GET", "https://x.example/a", time.monotonic() + 500.0)
    assert 0 < timeouts[0] <= 5.0
    assert timeouts[1] == 60.0  # capped at the old per-request ceiling


def test_sarvam_no_call_after_deadline():
    calls = []
    c = httpx.Client(transport=httpx.MockTransport(
        lambda r: calls.append(r) or httpx.Response(200)))
    with pytest.raises(ocr.SarvamError) as ei:
        ocr._sarvam_request(c, "GET", "https://x.example/a", time.monotonic() - 0.01)
    assert calls == [] and ei.value.outage
    assert "out of time" in str(ei.value)


def test_sarvam_timeout_at_deadline_is_not_retried(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        _sleep(0.3)
        raise httpx.ReadTimeout("slow", request=request)

    monkeypatch.setattr(ocr.time, "sleep", lambda s: None)
    c = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ocr.SarvamError) as ei:
        ocr._sarvam_request(c, "GET", "https://x.example/a", time.monotonic() + 0.2)
    assert len(calls) == 1 and ei.value.outage
    assert "out of time" in str(ei.value)


@pytest.mark.parametrize("status,outage", [(500, True), (502, True), (404, False)])
def test_sarvam_http_errors_flag_outage(status, outage):
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status)))
    with pytest.raises(ocr.SarvamError) as ei:
        ocr._sarvam_request(c, "GET", "https://x.example/a", time.monotonic() + 30)
    assert ei.value.outage is outage


def test_sarvam_503_after_retries_is_outage(monkeypatch):
    monkeypatch.setattr(ocr.time, "sleep", lambda s: None)
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(ocr.SarvamError) as ei:
        ocr._sarvam_request(c, "GET", "https://x.example/a", time.monotonic() + 100)
    assert ei.value.outage
