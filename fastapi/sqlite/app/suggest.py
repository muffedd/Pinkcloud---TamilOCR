"""AI fix: correction candidates for one Doubt word (POST /jobs/{id}/suggest).

A small provider seam. The route hands suggest() the word, its line and a
few neighbour lines; the configured provider returns up to MAX_CANDIDATES
readings; suggest() cleans them up (strip, dedupe, drop == before, clamp
score) and returns the contract shape.

Provider: Gemini (gemini-3.1-flash-lite), picked for V4. The feature is
OFF, and the route answers a quiet 503 without any network call, until
GEMINI_API_KEY is set in the server environment.

  SUGGEST_PROVIDER   unset/"gemini" = Gemini (needs GEMINI_API_KEY)
                     "sarvam"       = Sarvam chat (needs SARVAM_API_KEY)
                     "off"          = disabled even with a key set
Sarvam is an explicit opt-in only: SARVAM_API_KEY is already set for OCR,
and that alone must never start paid chat calls.

Other environment:
  SUGGEST_MODEL      override the model id. Defaults: gemini-3.1-flash-lite
                     (Gemini API, stable id), sarvam-105b (Sarvam chat).
  SUGGEST_TIMEOUT_S  per-request budget, default 12 (live Gemini calls run
                     2.5-4.3s with a slow tail; 8s cut off real answers).
  GEMINI_BASE_URL    default https://generativelanguage.googleapis.com
  SARVAM_BASE_URL    default https://api.sarvam.ai (shared with OCR)

Keys are read at call time and sent only in request headers (never in a
URL, so httpx's request log line cannot carry them). They are never
logged, stored or put into an error message: every provider failure
becomes SuggestUnavailable with a fixed message, and only the exception
type name is logged.
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger("pinkcloud.suggest")

MAX_CANDIDATES = 3
MAX_TEXT = 256

DEFAULT_MODELS = {
    "gemini": "gemini-3.1-flash-lite",
    "sarvam": "sarvam-105b",
}
_KEY_ENV = {"gemini": "GEMINI_API_KEY", "sarvam": "SARVAM_API_KEY"}

# Tests swap in an httpx.MockTransport here; None = real network.
_TRANSPORT = None


class SuggestUnavailable(RuntimeError):
    """The model can't answer right now (off, no key, timeout, bad reply).
    The route maps this to a 503 with a fixed, key-free message."""


def provider() -> str | None:
    """The active provider name, or None when AI fix is off."""
    name = (os.environ.get("SUGGEST_PROVIDER") or "gemini").strip().lower()
    if name not in _PROVIDERS:
        return None
    if not (os.environ.get(_KEY_ENV[name]) or "").strip():
        return None
    return name


def status() -> dict:
    """For /health: is AI fix live, and with what (never the key)."""
    name = provider()
    return {"ai_suggest": bool(name), "ai_suggest_provider": name}


def _key(name: str) -> str:
    return (os.environ.get(_KEY_ENV[name]) or "").strip()


def _model(name: str) -> str:
    return (os.environ.get("SUGGEST_MODEL") or "").strip() or DEFAULT_MODELS[name]


DEFAULT_TIMEOUT_S = 12.0


def _timeout() -> float:
    try:
        return max(1.0, float(os.environ.get("SUGGEST_TIMEOUT_S") or DEFAULT_TIMEOUT_S))
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _client():
    import httpx
    return httpx.Client(timeout=_timeout(), transport=_TRANSPORT)


# --- non-Tamil guard ---------------------------------------------------------
# AI fix reads Tamil only. Given a Latin/English word (or digits, symbols)
# the model tends to transliterate it or invent a Tamil word, so such a
# word never reaches the provider: suggest() answers an empty list ("AI
# found no better reading") with no network call. Same rule as the
# editor's aiSkipsWord(): no character in the Tamil block U+0B80-U+0BFF.

def has_tamil(text: str) -> bool:
    return any("\u0b80" <= ch <= "\u0bff" for ch in text or "")


# --- prompt -----------------------------------------------------------------

SYSTEM_PROMPT = (
    "You fix OCR errors in printed Tamil text (classical and modern). "
    "You get one suspect word, the OCR line it sits in, and nearby lines "
    "for context. Propose up to 3 corrected readings of THAT WORD ONLY, "
    "best first, each with a score from 0 to 1 for how likely it is "
    "right. Keep the script and spelling conventions of the source. If "
    "the word already looks correct, return an empty list. Reply with "
    'JSON only: {"candidates": [{"text": "...", "score": 0.0}]}'
)


def build_prompt(before: str, line_text: str, word: int,
                 neighbors_before: list[str], neighbors_after: list[str]) -> str:
    parts = []
    if neighbors_before:
        parts.append("Lines before:\n" + "\n".join(neighbors_before))
    parts.append(f"Target line:\n{line_text}")
    if neighbors_after:
        parts.append("Lines after:\n" + "\n".join(neighbors_after))
    parts.append(f"Suspect word (word {word} of the target line): {before}")
    return "\n\n".join(parts)


# Gemini structured-output schema (OpenAPI subset, as in the REST docs).
_GEMINI_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "candidates": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {"text": {"type": "STRING"},
                               "score": {"type": "NUMBER"}},
                "required": ["text", "score"],
            },
        },
    },
    "required": ["candidates"],
}


# --- providers ----------------------------------------------------------------
# Each takes (system, prompt) and returns the model's raw text reply, or
# raises. suggest() owns parsing and every error mapping.

def _call_gemini(system: str, prompt: str) -> str:
    base = (os.environ.get("GEMINI_BASE_URL")
            or "https://generativelanguage.googleapis.com").rstrip("/")
    url = f"{base}/v1beta/models/{_model('gemini')}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 512,
            "responseMimeType": "application/json",
            "responseSchema": _GEMINI_SCHEMA,
        },
    }
    with _client() as c:
        r = c.post(url, json=body, headers={"x-goog-api-key": _key("gemini")})
    r.raise_for_status()
    data = r.json()
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def _call_sarvam(system: str, prompt: str) -> str:
    base = (os.environ.get("SARVAM_BASE_URL") or "https://api.sarvam.ai").rstrip("/")
    body = {
        "model": _model("sarvam"),
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 512,
        "reasoning_effort": "low",
        "response_format": {"type": "json_object"},
    }
    key = _key("sarvam")
    with _client() as c:
        r = c.post(f"{base}/v1/chat/completions", json=body,
                   headers={"api-subscription-key": key,
                            "Authorization": f"Bearer {key}"})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"] or ""


_PROVIDERS = {"gemini": _call_gemini, "sarvam": _call_sarvam}


# --- parsing ------------------------------------------------------------------

def _extract_json(text: str) -> dict:
    """The reply should be pure JSON; tolerate a ```json fence or prose
    around one object."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in model reply")
    return json.loads(text[start:end + 1])


def clean_candidates(raw, before: str) -> list[dict]:
    """Contract shape: [{text, score 0..1, source: "llm"}], at most
    MAX_CANDIDATES, no blanks, no duplicates, nothing equal to `before`."""
    out: list[dict] = []
    seen = {before.strip()}
    items = raw.get("candidates") if isinstance(raw, dict) else None
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text or len(text) > MAX_TEXT or text in seen or len(text.split()) > 3:
            continue
        try:
            score = float(item.get("score", 0.5))
        except (TypeError, ValueError):
            score = 0.5
        if score != score:  # NaN
            score = 0.5
        seen.add(text)
        out.append({"text": text, "score": round(min(1.0, max(0.0, score)), 3),
                    "source": "llm"})
        if len(out) >= MAX_CANDIDATES:
            break
    return out


def suggest(before: str, line_text: str, word: int,
            neighbors_before: list[str] | None = None,
            neighbors_after: list[str] | None = None) -> list[dict]:
    """Candidates for one word. Raises SuggestUnavailable when the feature
    is off or the provider fails in any way."""
    name = provider()
    if name is None:
        raise SuggestUnavailable("AI suggestions are off")
    if not has_tamil(before):
        return []
    prompt = build_prompt(before, line_text, word,
                          neighbors_before or [], neighbors_after or [])
    try:
        reply = _PROVIDERS[name](SYSTEM_PROMPT, prompt)
        return clean_candidates(_extract_json(reply), before)
    except Exception as exc:
        # Type name only: an exception message (e.g. an HTTP error) could
        # echo request details, so it is never logged or returned.
        logger.warning("AI suggest via %s failed (%s)", name, type(exc).__name__)
        raise SuggestUnavailable("AI suggestions unavailable") from None
