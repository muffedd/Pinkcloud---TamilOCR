"""Tests for app/textcheck.py (module D: per-line text-quality proxy).

Run from fastapi/sqlite/:  python -m pytest tests/test_textcheck.py -v
Needs only the standard library + pytest (no Paddle, no Sarvam, no web app).
Real lines come from ocr_outputs/sarvam/raw/sample*/sample*.png/metadata/page_001.json.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.textcheck import line_signals, orphan_signs, score_line, tamil_share

REPO = Path(__file__).resolve().parents[3]
SARVAM = REPO / "ocr_outputs" / "sarvam" / "raw"

# Real Sarvam lines (sample1 = Narrinai commentary page, sample2 = old prose page).
CLEAN_REAL = [
    "எ - து, தலைவன் பிரியக்கருதிய தறிந்த தோழி தலைவியிடங் கூற",
    "நின்ற சொல்லர் நீடுதோன் றினிய",
    "சிறுமை யுறுபவோ செய்பறி யலரே.",
    "உரை:- தோழி, நம் காதலர் நிலைமை தவறாத வாய்மையுடையவர்;",
    "ஐயா கவுசிகராஜனை காதிராஜன் பெற்ற வய",           # Grantha ஜ
    "கொண்டுஉங்களிஷ்டப்படிக்கேவரங்கொடுத்தோ",         # run-together old print
]
# Sarvam's hallucinated caption for blank/illegible blocks (sample2 p1-b1).
HALLUCINATION = ("This image does not contain any legible text. It appears to be "
                 "a blurry, abstract pattern")


def _sarvam_lines():
    lines = []
    for path in sorted(SARVAM.glob("sample*/sample*.png/metadata/page_001.json")):
        for block in json.loads(path.read_text(encoding="utf-8"))["blocks"]:
            lines += [line for line in block["text"].splitlines() if line.strip()]
    return lines


# ---- scale contract -----------------------------------------------------------

@pytest.mark.parametrize("line", CLEAN_REAL)
def test_clean_real_tamil_scores_high(line):
    assert score_line(line) == pytest.approx(0.99)


def test_orphan_vowel_sign_drops_to_about_0_9():
    assert score_line("ாதவறு") == pytest.approx(0.90)
    # orphan sign spliced into a real line
    broken = "நின்ற சொல்லர் ் நீடுதோன் றினிய"
    assert 0.85 <= score_line(broken) <= 0.92


def test_odd_character_drops_to_about_0_9():
    assert score_line("அவன் வந்தான்\ufffd") == pytest.approx(0.90)
    assert score_line("தம்மின் றமையா \u00a7 நந்நயந் தருளி") == pytest.approx(0.90)


def test_more_flaws_score_lower_but_readable_line_stays_above_floor():
    one = score_line("ாதவறு")
    three = score_line("ாதவறு ாக ீம")
    assert three < one
    assert three >= 0.6


def test_repeated_sentence_is_penalised():
    # the dedup artifact text_filter.clean() removes
    score = score_line("வணக்கம். வணக்கம். வணக்கம்.")
    assert 0.8 <= score < 0.99


def test_real_hallucination_line_scores_low():
    assert score_line(HALLUCINATION) <= 0.1


def test_low_tamil_share_is_at_most_0_6():
    assert score_line("hello world") == 0.0
    assert score_line("அவன் hello world") <= 0.6


def test_mixed_line_above_half_tamil_is_between():
    score = score_line("அவன் வந்தான் இன்று காலை ok")
    assert 0.6 < score < 0.99


def test_garbage_is_at_most_0_6():
    assert score_line("அ\ufffd\ufffd\ufffd") <= 0.6          # mostly odd chars
    assert score_line("கா கா கா கா கா") <= 0.6               # repetition loop
    assert score_line("123 - 45 / 67") <= 0.6                # no letters at all


def test_empty_and_none():
    assert score_line("") == 0.0
    assert score_line("   ") == 0.0
    assert score_line(None) == 0.0


# ---- whole real sample ------------------------------------------------------------

def test_every_real_sarvam_line_is_in_range_and_tamil_lines_pass_review_floor():
    lines = _sarvam_lines()
    assert len(lines) > 40
    for line in lines:
        score = score_line(line)
        assert 0.0 <= score <= 1.0
        assert isinstance(score, float)
        share = tamil_share(line)
        if share is not None and share >= 0.95:
            assert score >= 0.5, line          # export CONFIDENCE_REVIEW_FLOOR
        if share == 0.0:
            assert score < 0.5, line           # hallucinated English -> review


# ---- ported helpers ---------------------------------------------------------------

def test_orphan_signs_matches_text_filter():
    assert orphan_signs("கா") == [] and orphan_signs("ாத") == [1]
    assert orphan_signs("கொ") == []                       # decomposed form
    assert orphan_signs("் அ") == [1]                      # stray virama


def test_line_signals_shape():
    s = line_signals("ாதவறு")
    assert set(s) == {"tamil_share", "orphan_signs", "odd_chars",
                      "repeated_sentences", "repetition_loop"}
    assert s["orphan_signs"] == [1]


def test_deterministic():
    assert score_line(CLEAN_REAL[0]) == score_line(CLEAN_REAL[0])


def test_importable_standalone_without_app_deps():
    code = ("import sys; sys.modules['cv2'] = None; sys.modules['paddleocr'] = None; "
            "sys.modules['fastapi'] = None; sys.modules['numpy'] = None; "
            "import importlib.util, pathlib; "
            "spec = importlib.util.spec_from_file_location('textcheck', 'app/textcheck.py'); "
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
            "print(m.score_line('அவன் வந்தான்.'))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=Path(__file__).resolve().parents[1], check=True)
    assert out.stdout.strip() == "0.99"
