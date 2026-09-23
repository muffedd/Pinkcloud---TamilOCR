"""Per-line text-quality proxy for OCR output (module D of the Sarvam integration).

Sarvam returns no per-line recognition confidence, so the backend needs another
0..1 number to put in a line's "confidence" field. score_line() looks only at
the text itself, using the same signals as text_filter.py at the repo root
(clean() and orphan_signs()):

  * Tamil share   - Tamil letters / all script letters (whitespace, digits and
                    punctuation are ignored, as in text_filter.clean()).
  * orphan signs  - a Tamil vowel sign (or virama) that does not follow a
                    consonant, the orphan_signs() check. Usually a broken glyph.
  * odd chars     - characters that do not belong in printed Tamil: U+FFFD,
                    control / private-use / format chars, stray symbols,
                    unassigned code points in the Tamil block.
  * dedup         - a sentence repeated inside the line (the artifact
                    text_filter.clean() removes) or a word repeated in a loop.

Score scale (agreed contract):
  ~0.99        clean Tamil
  ~0.9         a few orphan vowel signs, odd chars or one repeated sentence
  <= 0.6       low Tamil share, no letters, or garbage (many odd chars,
               repetition loops)
  0.0          empty line

Pure standard library: importable without Sarvam or the web app.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["score_line", "line_signals", "orphan_signs", "tamil_share"]

# ---- character classes (ported from text_filter.py) ------------------------
TAMIL = re.compile(r"[\u0b80-\u0bff]")
# Vowel signs only; exclude anusvara and virama (text_filter.SIGNS).
SIGNS = set(range(0x0BBE, 0x0BCD)) | {0x0BD7}
CONS = set(range(0x0B95, 0x0BBA))  # includes Grantha ஜ ஷ ஸ ஹ
VIRAMA = 0x0BCD
SENTENCES = re.compile(r"[^.!?\u0964\u0965]+[.!?\u0964\u0965]*")
WORD = re.compile(r"\S+")

# Punctuation seen in printed Tamil text besides plain ASCII.
TYPOGRAPHIC = set("\u2010\u2011\u2012\u2013\u2014\u2015\u2018\u2019\u201c\u201d"
                  "\u2022\u2026\u00ab\u00bb\u00b7\u00a0")
JOINERS = {"\u200c", "\u200d"}  # ZWNJ / ZWJ are legitimate in Tamil text

# ---- score mapping constants ---------------------------------------------------
CLEAN = 0.99
LOW_CAP = 0.6          # ceiling for low-share / garbage lines
SHARE_OK = 0.95        # share at or above this counts as fully Tamil
SHARE_MIN = 0.5        # text_filter.clean() drop threshold
FIRST_FLAW = 0.09      # first orphan sign / odd char / repeat: 0.99 -> 0.90
EXTRA_FLAW = 0.04      # each further flaw
FLAW_FLOOR = 0.62      # flaws alone never push a readable line below this
GARBAGE_RATIO = 0.2    # odd chars / visible chars above this => garbage


def tamil_share(text: str) -> float | None:
    """Tamil letters / script letters, or None when the line has no letters."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return None
    return sum(bool(TAMIL.fullmatch(c)) for c in letters) / len(letters)


def orphan_signs(text: str) -> list[int]:
    """1-based columns of vowel signs / virama not attached to a consonant.

    Same rule as text_filter.orphan_signs() (a vowel sign may follow a consonant
    or another sign, for decomposed forms like கொ), extended to a virama that
    follows neither a consonant nor a sign.
    """
    out = []
    for i, ch in enumerate(text):
        code = ord(ch)
        if code in SIGNS or code == VIRAMA:
            prev = ord(text[i - 1]) if i else None
            if prev is None or prev not in CONS | SIGNS:
                out.append(i + 1)
    return out


def _odd_chars(text: str) -> list[int]:
    """1-based columns of characters that don't belong in printed Tamil."""
    out = []
    for i, ch in enumerate(text):
        code = ord(ch)
        if ch.isspace() or ch in JOINERS or ch in TYPOGRAPHIC:
            continue
        if 0x0B80 <= code <= 0x0BFF:
            if unicodedata.category(ch) == "Cn":  # unassigned slot in block
                out.append(i + 1)
            continue
        if code < 0x80:
            if ch.isprintable():
                continue  # ASCII letters, digits, punctuation
            out.append(i + 1)  # control char
            continue
        if ch.isalpha():
            continue  # other-script letters are handled by tamil_share
        out.append(i + 1)  # U+FFFD, symbols, private use, format chars, ...
    return out


def _repeats(text: str) -> tuple[int, bool]:
    """(repeated sentences, repetition loop) inside one line."""
    seen, repeated = set(), 0
    for match in SENTENCES.finditer(text):
        key = match.group().strip()
        if key and key in seen:
            repeated += 1
        elif key:
            seen.add(key)
    words = WORD.findall(text)
    loop = False
    if len(words) >= 4:
        top = max(words.count(w) for w in set(words))
        loop = top >= 3 and top / len(words) > 0.5
    return repeated, loop


def line_signals(text: str) -> dict:
    """The raw signals behind score_line(), for debugging and review UIs."""
    text = text or ""
    repeated, loop = _repeats(text)
    return {
        "tamil_share": tamil_share(text),
        "orphan_signs": orphan_signs(text),
        "odd_chars": _odd_chars(text),
        "repeated_sentences": repeated,
        "repetition_loop": loop,
    }


def score_line(text: str) -> float:
    """Return a 0..1 text-quality score for one OCR line (4 decimals).

    Deterministic, no side effects. None / empty / whitespace-only -> 0.0.
    """
    if not text or not text.strip():
        return 0.0
    s = line_signals(text)
    visible = sum(not c.isspace() for c in text)

    # 1. Base score from Tamil share.
    share = s["tamil_share"]
    if share is None:                       # only digits / punctuation / symbols
        base = 0.3
    elif share >= SHARE_OK:
        base = CLEAN
    elif share >= SHARE_MIN:                # mixed line: 0.75 .. 0.99
        base = 0.75 + (CLEAN - 0.75) * (share - SHARE_MIN) / (SHARE_OK - SHARE_MIN)
    else:                                   # mostly non-Tamil: 0 .. 0.6
        base = LOW_CAP * share / SHARE_MIN

    # 2. Flaw penalties: 0.09 for the first flaw, 0.04 for each further one.
    flaws = len(s["orphan_signs"]) + len(s["odd_chars"]) + s["repeated_sentences"]
    score = base
    if flaws:
        score = max(base - FIRST_FLAW - EXTRA_FLAW * (flaws - 1), min(base, FLAW_FLOOR))

    # 3. Garbage caps.
    if len(s["odd_chars"]) / max(visible, 1) > GARBAGE_RATIO or s["repetition_loop"]:
        score = min(score, LOW_CAP * 0.9)   # 0.54

    return round(max(0.0, min(1.0, score)), 4)
