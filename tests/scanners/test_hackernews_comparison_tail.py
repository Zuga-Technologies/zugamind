"""A vendor named as the thing being BEATEN did not ship anything.

Regression for the 2026-09-10 16:06Z paid wake: "Cognition launches new SWE-2
model, Rivaling Fable 5.1 and GPT-Astra" took the PROMOTED lane (relevance 0.9,
bid 0.68) because _is_vendor_ship's two halves matched two DIFFERENT subjects --
the ship verb belonged to Cognition (not a vendor we build on) and the vendor
token "GPT" came from the comparison tail.

Neither prior narrowing covers this shape: the title carries a real ship verb,
and it does not open with "How".
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "zugamind"))

from scanners.world import hackernews  # noqa: E402


# The wake this file closes, plus the same shape from other rivals.
RIVAL_LAUNCHES = [
    ("Cognition launches new SWE-2 model, Rivaling Fable 5.1 and GPT-Astra",
     "https://cognition.com/blog/swe-2"),
    ("Cursor 2.0 ships, now beating Claude Code on SWE-bench",
     "https://cursor.com/blog/2-0"),
    ("Windsurf launches Cascade, an alternative to Codex",
     "https://windsurf.com/blog/cascade"),
    ("Zed ships an agent panel, a competitor to Claude Code",
     "https://zed.dev/blog/agent"),
    ("Mistral releases Devstral, cheaper than GPT-Astra",
     "https://example.test/devstral"),
]

# Must stay promoted: the vendor is the SUBJECT, comparison tail or not.
REAL_VENDOR_SHIPS = [
    ("Anthropic launches Claude Opus 5", "https://anthropic.com/news/opus-5"),
    ("OpenAI releases GPT-6 Astra", "https://openai.com/index/gpt-6-astra"),
    ("Anthropic ships Opus 5, outperforming GPT-Astra",
     "https://anthropic.com/news/opus-5"),
    ("Claude Code is going to reduce limits by 25%",
     "https://news.example.test/limits"),
    ("OpenAI deprecates the Assistants API", "https://openai.com/index/dep"),
    # Opens with the marker, so there is no subject span to narrow to: judged
    # whole, exactly as before.
    ("Rivaling GPT-6: Anthropic ships Claude Opus 5",
     "https://anthropic.com/news/opus-5"),
]


def test_rival_launch_is_not_a_vendor_ship():
    for title, url in RIVAL_LAUNCHES:
        assert not hackernews._is_vendor_ship(title, url), title


def test_real_vendor_ships_still_promote():
    for title, url in REAL_VENDOR_SHIPS:
        assert hackernews._is_vendor_ship(title, url), title


def test_ship_subject_stops_at_the_comparison_marker():
    assert hackernews._ship_subject(
        "Cognition launches new SWE-2 model, Rivaling Fable 5.1 and GPT-Astra"
    ) == "Cognition launches new SWE-2 model, "


def test_ship_subject_returns_whole_title_when_marker_opens_it():
    title = "Rivaling GPT-6: Anthropic ships Claude Opus 5"
    assert hackernews._ship_subject(title) == title


def test_ship_subject_returns_whole_title_when_no_marker():
    title = "Anthropic launches Claude Opus 5"
    assert hackernews._ship_subject(title) == title
