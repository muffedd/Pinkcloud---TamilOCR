"""POST /jobs/{id}/suggest - AI fix candidates behind a provider seam.

No test here touches the network: providers are exercised through an
httpx.MockTransport, and the default path (no provider picked) must not
make any HTTP call at all.
"""

import json
import logging
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, storage, suggest
from app.main import app

KEY = "sk-test-DO-NOT-LEAK-7f3a"
LINES = ["அகர முதல எழுத்தெல்லாம்", "ஆதி பகவன் முதற்றே உலகு",
         "கற்றதனால் ஆய பயனென்கொல் வாழறிவன்", "நற்றாள் தொழாஅர் எனின்",
         "மலர்மிசை ஏகினான் மாணடி", "சேர்ந்தார் நிலமிசை நீடுவாழ் வார்"]


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("suggest")
    db.DB_PATH = tmp / "test.db"
    storage.UPLOAD_ROOT = tmp / "uploads"
    db.init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("SUGGEST_PROVIDER", "SUGGEST_MODEL", "GEMINI_API_KEY",
                "SARVAM_API_KEY", "GEMINI_BASE_URL", "SARVAM_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(suggest, "_TRANSPORT", httpx.MockTransport(_no_network))
    yield


def _no_network(request):  # pragma: no cover - reaching it is the failure
    raise AssertionError(f"unexpected HTTP call to {request.url}")


def _done_job(status="done"):
    job_id = uuid.uuid4().hex
    db.create_job(job_id, "kural.png", "0" * 64)
    lines = [{"id": f"L{i}", "seq": i, "body": b, "bbox": [0, i * 40, 500, 36],
              "confidence": 0.5, "needs_review": True}
             for i, b in enumerate(LINES, start=1)]
    result = {"pages": [{"page": 1, "profile": "LIGHT", "lines": lines}]}
    db.set_result(job_id, status, json.dumps(result))
    return job_id


REQ = {"page": 1, "line": "L3", "word": 5, "before": "வாழறிவன்",
       "context": LINES[2]}


class Recorder:
    def __init__(self, reply):
        self.reply = reply
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.reply(request) if callable(self.reply) else self.reply


def _gemini_reply(payload):
    text = json.dumps(payload, ensure_ascii=False)
    return httpx.Response(200, json={"candidates": [
        {"content": {"parts": [{"text": text}], "role": "model"}}]})


def _use(monkeypatch, provider, recorder):
    monkeypatch.setenv("SUGGEST_PROVIDER", provider)
    monkeypatch.setenv("GEMINI_API_KEY" if provider == "gemini" else "SARVAM_API_KEY", KEY)
    monkeypatch.setattr(suggest, "_TRANSPORT", httpx.MockTransport(recorder))


# --- off by default --------------------------------------------------------

def test_off_by_default_is_quiet_503(client):
    job = _done_job()
    r = client.post(f"/jobs/{job}/suggest", json=REQ)
    assert r.status_code == 503
    assert r.json() == {"detail": "AI suggestions unavailable"}
    h = client.get("/health").json()
    assert h["ai_suggest"] is False and h["ai_suggest_provider"] is None


def test_sarvam_ocr_key_alone_does_not_turn_it_on(client, monkeypatch):
    """SARVAM_API_KEY is set for OCR anyway; that must not start chat spend."""
    monkeypatch.setenv("SARVAM_API_KEY", KEY)
    r = client.post(f"/jobs/{_done_job()}/suggest", json=REQ)
    assert r.status_code == 503


def test_gemini_key_turns_it_on_by_default(client, monkeypatch):
    rec = Recorder(_gemini_reply({"candidates": []}))
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    monkeypatch.setattr(suggest, "_TRANSPORT", httpx.MockTransport(rec))
    r = client.post(f"/jobs/{_done_job()}/suggest", json=REQ)
    assert r.status_code == 200 and len(rec.requests) == 1


def test_off_switch_beats_key(client, monkeypatch):
    monkeypatch.setenv("SUGGEST_PROVIDER", "off")
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    assert client.post(f"/jobs/{_done_job()}/suggest", json=REQ).status_code == 503


@pytest.mark.parametrize("provider", ["gemini", "sarvam"])
def test_provider_without_key_is_off(client, monkeypatch, provider):
    monkeypatch.setenv("SUGGEST_PROVIDER", provider)
    r = client.post(f"/jobs/{_done_job()}/suggest", json=REQ)
    assert r.status_code == 503


def test_unknown_provider_is_off(client, monkeypatch):
    monkeypatch.setenv("SUGGEST_PROVIDER", "openai")
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    assert client.post(f"/jobs/{_done_job()}/suggest", json=REQ).status_code == 503


# --- gemini -----------------------------------------------------------------

def test_gemini_candidates_cleaned_and_request_shape(client, monkeypatch):
    rec = Recorder(_gemini_reply({"candidates": [
        {"text": "வாலறிவன்", "score": 0.91},
        {"text": "வாலறிவன்", "score": 0.5},      # duplicate
        {"text": "வாழறிவன்", "score": 0.4},      # == before
        {"text": " வாளறிவன் ", "score": 3},      # stripped, score clamped
        {"text": "", "score": 0.9},             # blank
        {"text": "வாரறிவன்", "score": 0.2},
        {"text": "extra", "score": 0.1},        # over the cap of 3
    ]}))
    _use(monkeypatch, "gemini", rec)
    r = client.post(f"/jobs/{_done_job()}/suggest", json=REQ)
    assert r.status_code == 200
    assert r.json() == {"candidates": [
        {"text": "வாலறிவன்", "score": 0.91, "source": "llm"},
        {"text": "வாளறிவன்", "score": 1.0, "source": "llm"},
        {"text": "வாரறிவன்", "score": 0.2, "source": "llm"},
    ]}
    (req,) = rec.requests
    assert req.url.path == "/v1beta/models/gemini-3.1-flash-lite:generateContent"
    assert req.headers["x-goog-api-key"] == KEY
    assert KEY not in str(req.url)
    sent = json.loads(req.content)
    prompt = sent["contents"][0]["parts"][0]["text"]
    # target line + 2 neighbours each side; L6 is out of range
    for i in range(0, 5):
        assert LINES[i] in prompt
    assert LINES[5] not in prompt
    assert "word 5" in prompt and "வாழறிவன்" in prompt
    assert sent["generationConfig"]["responseMimeType"] == "application/json"
    h = client.get("/health").json()
    assert h["ai_suggest"] is True and h["ai_suggest_provider"] == "gemini"
    assert KEY not in json.dumps(h)


def test_model_override(client, monkeypatch):
    rec = Recorder(_gemini_reply({"candidates": []}))
    _use(monkeypatch, "gemini", rec)
    monkeypatch.setenv("SUGGEST_MODEL", "gemini-3.5-flash-lite")
    r = client.post(f"/jobs/{_done_job()}/suggest", json=REQ)
    assert r.status_code == 200 and r.json() == {"candidates": []}
    assert "gemini-3.5-flash-lite:generateContent" in rec.requests[0].url.path


def test_fenced_json_reply_is_accepted(client, monkeypatch):
    rec = Recorder(httpx.Response(200, json={"candidates": [{"content": {"parts": [
        {"text": 'Sure:\n```json\n{"candidates": [{"text": "முதல", "score": 0.8}]}\n```'}]}}]}))
    _use(monkeypatch, "gemini", rec)
    body = dict(REQ, line="L1", word=2, before="ழுதல")
    r = client.post(f"/jobs/{_done_job()}/suggest", json=body)
    assert r.json()["candidates"] == [{"text": "முதல", "score": 0.8, "source": "llm"}]


# --- sarvam -----------------------------------------------------------------

def test_sarvam_request_shape(client, monkeypatch):
    content = json.dumps({"candidates": [{"text": "வாலறிவன்", "score": 0.7}]},
                         ensure_ascii=False)
    rec = Recorder(httpx.Response(200, json={"choices": [
        {"index": 0, "finish_reason": "stop",
         "message": {"role": "assistant", "content": content}}]}))
    _use(monkeypatch, "sarvam", rec)
    r = client.post(f"/jobs/{_done_job()}/suggest", json=REQ)
    assert r.status_code == 200
    assert r.json()["candidates"][0]["text"] == "வாலறிவன்"
    (req,) = rec.requests
    assert str(req.url) == "https://api.sarvam.ai/v1/chat/completions"
    assert req.headers["api-subscription-key"] == KEY
    sent = json.loads(req.content)
    assert sent["model"] == "sarvam-105b"
    assert sent["messages"][0]["role"] == "system"


# --- degrade quietly, never leak the key -------------------------------------

@pytest.mark.parametrize("reply", [
    httpx.Response(500, text=f"upstream exploded, key={KEY}"),
    httpx.Response(429, json={"error": "rate limited"}),
    httpx.Response(200, text="not json at all"),
    httpx.Response(200, json={"unexpected": "shape"}),
])
def test_provider_failure_is_503_without_details(client, monkeypatch, caplog, reply):
    _use(monkeypatch, "gemini", Recorder(reply))
    with caplog.at_level(logging.DEBUG):
        r = client.post(f"/jobs/{_done_job()}/suggest", json=REQ)
    assert r.status_code == 503
    assert r.json() == {"detail": "AI suggestions unavailable"}
    assert KEY not in r.text
    assert KEY not in caplog.text


def test_timeout_is_503(client, monkeypatch, caplog):
    def boom(request):
        raise httpx.ReadTimeout("timed out", request=request)
    _use(monkeypatch, "sarvam", Recorder(boom))
    with caplog.at_level(logging.DEBUG):
        r = client.post(f"/jobs/{_done_job()}/suggest", json=REQ)
    assert r.status_code == 503
    assert KEY not in caplog.text


# --- routing / validation ----------------------------------------------------

def test_unknown_job_page_line(client, monkeypatch):
    _use(monkeypatch, "gemini", Recorder(_gemini_reply({"candidates": []})))
    assert client.post("/jobs/" + "f" * 32 + "/suggest", json=REQ).status_code == 404
    assert client.post("/jobs/not-a-job/suggest", json=REQ).status_code == 404
    job = _done_job()
    assert client.post(f"/jobs/{job}/suggest", json=dict(REQ, page=2)).status_code == 404
    no_ctx = {k: v for k, v in REQ.items() if k != "context"}
    assert client.post(f"/jobs/{job}/suggest",
                       json=dict(no_ctx, line="L99")).status_code == 404


def test_missing_line_falls_back_to_request_context(client, monkeypatch):
    rec = Recorder(_gemini_reply({"candidates": []}))
    _use(monkeypatch, "gemini", rec)
    r = client.post(f"/jobs/{_done_job()}/suggest",
                    json=dict(REQ, line="L99", context="ஒரு வரி வாழறிவன்"))
    assert r.status_code == 200
    prompt = json.loads(rec.requests[0].content)["contents"][0]["parts"][0]["text"]
    assert "ஒரு வரி வாழறிவன்" in prompt and LINES[0] not in prompt


def test_unfinished_job_is_409(client, monkeypatch):
    _use(monkeypatch, "gemini", Recorder(_gemini_reply({"candidates": []})))
    job = uuid.uuid4().hex
    db.create_job(job, "x.png", "0" * 64)
    assert client.post(f"/jobs/{job}/suggest", json=REQ).status_code == 409


@pytest.mark.parametrize("bad", [
    dict(REQ, page=0), dict(REQ, word=0), dict(REQ, before="   "),
    {k: v for k, v in REQ.items() if k != "before"},
])
def test_validation(client, bad):
    assert client.post(f"/jobs/{_done_job()}/suggest", json=bad).status_code == 422
