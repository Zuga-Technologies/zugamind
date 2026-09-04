"""A customer case study is a case study whatever verb it uses.

Regression for the 2026-09-03 21:09Z wake: [openai] "Legora reviewed 41
documents in minutes with GPT-6 Astra" bid **0.68** against a 0.590 floor and
bought a Claude session. 0.68 is HIGH -- the tier reserved for launches,
pricing and deprecations -- and it was awarded to an ad, because
`_HIGH_RELEVANCE_RE` found the token "GPT-6" in the title after every
promotion rule had missed.

The miss was one word. `_PROMO_OUTCOME_RE` matched the buyer ("Legora", a
capitalised third-party proper noun at the head) and it matched the tail
("with GPT-6 Astra"). It failed only because "reviewed" was absent from a
hand-written list of outcome verbs -- a list that has to enumerate every way
a customer can be described doing something, which is every verb there is.

`_PROMO_CASE_STUDY_RE` had already learned the lesson for the sibling
"How ..." grammar and said so in its own comment: the discriminator is the
TAIL, not the verb. This is that principle applied to the grammar three lines
above it.

The other half of these tests is the price of applying it. Opening the verb
slot without guarding the HEAD demotes the lab's own launches -- measured,
"Introducing agentic video understanding with Gemini" and "Introducing CARE-X:
Towards Clinically Useful Radiology VLMs with Auxiliary Supervision..." both
flip HIGH -> NON-WORK on an unguarded version. NON-WORK is checked first and
wins over HIGH, so that failure is not a missed wake, it is deafness.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "zugamind") not in sys.path:
    sys.path.insert(0, str(_ROOT / "zugamind"))

from zugamind.scanners.world import ai_labs  # noqa: E402

import pytest  # noqa: E402


def _bid(title: str, summary: str = "", lab: str = "", urgency: float = 0.25) -> float:
    """WorldSignals' price for a trigger, so assertions are in the units that
    actually decide whether a session gets bought."""
    return round(0.25 + 0.4 * ai_labs._relevance_for(title, summary, lab) + 0.2 * urgency, 4)


def test_the_wake_that_caused_this_no_longer_clears_the_floor():
    title = "Legora reviewed 41 documents in minutes with GPT-6 Astra"
    summary = (
        "Legora used GPT-6 Astra to review 41 documents in minutes, find all "
        "four planted errors, and improve performance by nearly 40% in this "
        "financial-review workflow."
    )
    assert ai_labs._is_non_work(f"{title} {summary}")
    assert ai_labs._relevance_for(title, summary, "openai") == ai_labs._RELEVANCE_NON_WORK
    # the headline alone is enough -- openai's RSS summary is not guaranteed
    assert ai_labs._relevance_for(title) == ai_labs._RELEVANCE_NON_WORK
    # it bid 0.68 fresh, the top of the scale. Now it cannot reach any floor
    # this deployment has ever calibrated (0.5042 - 0.6224 in the record).
    assert _bid(title, summary, "openai") == pytest.approx(0.46)


@pytest.mark.parametrize("title", [
    # the wake, and a second live-cache ad found by the same measurement
    "Legora reviewed 41 documents in minutes with GPT-6 Astra",
    "ATV Big Air Tour turned 3 days of work into 3 hours with ChatGPT",
    # the 2026-08-19 14:29Z wake, whose own test docstring records that this
    # grammar did NOT catch it -- only the summary's "powered by" did. The
    # title now fails on its own, so a promo without that phrase still loses.
    "Replit expands access to software creation with GPT-5.6 Luna",
    # siblings through verbs no whitelist would have carried
    "Ramp drafted 900 vendor agreements overnight with Claude Opus 5",
    "Zendesk resolves 60% of tickets automatically with Gemini 3 Pro",
    "Bench analysed a decade of filings with GPT-6 Astra",
    "Notion Labs migrates its search stack with Codex",
])
def test_a_case_study_loses_its_tier_whatever_verb_it_uses(title):
    assert ai_labs._relevance_for(title) == ai_labs._RELEVANCE_NON_WORK
    assert _bid(title) < 0.510  # under every floor in floor_calibration.json


@pytest.mark.parametrize("title", [
    # the two that an unguarded verb slot demoted, measured on the live cache
    "Introducing agentic video understanding with Gemini",
    ("Introducing CARE-X: Towards Clinically Useful Radiology VLMs with "
     "Auxiliary Supervision, Reward-Aligned Learning, and Tool-Augmented "
     "Measurement"),
    # first-party subjects: the lab is not its own customer
    "OpenAI ships structured outputs with GPT-6 Astra",
    "Anthropic expands the Batch API with Claude Opus 5",
    "Announcing Gemini 3.8 Flash with Deep Think",
    "Deprecating o3 with a migration guide for GPT-6",
    "Previewing Ultrafast mode: GPT-5.6 Sol at up to 14X the speed",
])
def test_the_head_guard_keeps_the_labs_own_shipping_news(title):
    """The failure mode that costs more than a wasted wake. NON-WORK is
    checked FIRST and wins over HIGH, so a promotion rule that overreaches
    does not merely mis-price a launch -- it silences it."""
    assert ai_labs._relevance_for(title) != ai_labs._RELEVANCE_NON_WORK


def test_the_tail_is_still_required():
    """The head guard is not the whole rule. Without a `with <Capital>` tail
    naming a product, an ordinary sentence-case headline is not a case study
    and must keep its tier."""
    for title in (
        "Legora reviewed 41 documents in minutes",          # no tail
        "Legora reviewed 41 documents with unusual care",   # lowercase tail
        "Structured outputs now available in the API",      # a launch
    ):
        assert ai_labs._relevance_for(title) != ai_labs._RELEVANCE_NON_WORK, title
