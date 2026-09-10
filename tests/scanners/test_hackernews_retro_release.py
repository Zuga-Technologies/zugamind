"""A past release REFERRED TO is not a release ANNOUNCED.

Regression for the 2026-09-10 23:42Z paid wake: "OpenAI's Navier-Stokes
release included a Lean 4 formal proof" took the PROMOTED lane (relevance 0.9,
bid 0.70) on a stranger's blog, because `_SHIP_EVENT_RE`'s `\brelease[sd]?\b`
-- added for the finite VERB forms -- also matches the bare NOUN.

No prior narrowing covers this shape. OpenAI IS the subject, and OpenAI DID
ship; the missing axis is tense. The gate now masks a backward-looking
"release" noun and re-asks the grammar, so a title still promotes on any OTHER
evidence it carries.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "zugamind"))

from scanners.world import hackernews  # noqa: E402

_STRANGER = "https://www.johndcook.com/blog/2026/09/09/formal-method-revolution/"


def test_the_measured_title_no_longer_promotes():
    assert hackernews._is_vendor_ship(
        "OpenAI’s Navier-Stokes release included a Lean 4 formal proof",
        _STRANGER,
    ) is False


def test_retrospective_noun_is_the_only_thing_masked():
    # The noun goes; every other character stays, so no span shifts.
    masked = hackernews._mask_retro_release(
        "OpenAI’s Navier-Stokes release included a Lean 4 formal proof")
    assert "release" not in masked.lower()
    assert masked.startswith("OpenAI’s Navier-Stokes")
    assert masked.endswith("included a Lean 4 formal proof")
    assert len(masked) == len(
        "OpenAI’s Navier-Stokes release included a Lean 4 formal proof")


def test_determiner_forms_are_retrospective_too():
    for title in (
        "The GPT-6 release was quietly walked back",
        "What that Claude release actually changed",
        "A closer look at its Gemini release",
    ):
        assert hackernews._is_vendor_ship(title, _STRANGER) is False, title


def test_finite_verb_forms_still_promote():
    # These are the rows the `release[sd]?` alternation was ADDED for.
    for title in (
        "OpenAI releases GPT-6 Astra",
        "Anthropic released Claude Opus 5 today",
    ):
        assert hackernews._is_vendor_ship(title, _STRANGER) is True, title


def test_masking_not_vetoing_other_evidence_survives():
    # Same possessive-noun shape, but the title carries a real claim beside it.
    assert hackernews._is_vendor_ship(
        "OpenAI’s release of GPT-6 introduces new pricing", _STRANGER
    ) is True


def test_earlier_narrowings_are_not_regressed():
    assert hackernews._is_vendor_ship(
        "Anthropic launches Claude Opus 5", _STRANGER) is True
    assert hackernews._is_vendor_ship(
        "How GPT-5.6 Sol helps run quantum computing experiments",
        _STRANGER) is False
    assert hackernews._is_vendor_ship(
        "Cognition launches new SWE-2 model, Rivaling Fable 5.1 and GPT-Astra",
        _STRANGER) is False
    assert hackernews._is_vendor_ship(
        "Claude Opus 5", "https://www.anthropic.com/news/claude-opus-5") is True
