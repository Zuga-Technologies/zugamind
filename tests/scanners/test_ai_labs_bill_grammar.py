"""A lab endorsing a named piece of pending legislation is public affairs.

Regression for the 2026-09-01 05:51Z wake: [openai] "OpenAI supports
California's bill to advance youth AI safety" (SB 1119) bid 0.600 against a
0.563 floor and bought a Claude session. Public affairs is the demotion tier
this post belongs to and the one the scanner has had since 2026-08-18 -- the
miss was purely vocabulary. `_PUBLIC_AFFAIRS_RE` knew "legislation",
"lawmaker", "congress" and "ai act", and did not know "bill".

The other half of these tests is the reason the fix is grammar and not the
bare word: "bill" is also the invoice sense, and pricing is HIGH tier. A
demotion that reaches billing copy silences price changes -- strictly worse
than the wake it prevents.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "zugamind") not in sys.path:
    sys.path.insert(0, str(_ROOT / "zugamind"))

from zugamind.scanners.world import ai_labs  # noqa: E402


def _bid(title: str, summary: str = "", lab: str = "", urgency: float = 0.25) -> float:
    """WorldSignals' price for a trigger, so the assertions are in the units
    that actually decide whether a session gets bought."""
    return round(0.25 + 0.4 * ai_labs._relevance_for(title, summary, lab) + 0.2 * urgency, 4)


def test_the_wake_that_caused_this_no_longer_clears_the_floor():
    title = "OpenAI supports California’s bill to advance youth AI safety"
    summary = (
        "OpenAI supports California SB 1119, advancing strong, age-appropriate "
        "AI safeguards for teens while preserving opportunities to learn, "
        "create, and explore."
    )
    assert ai_labs._is_non_work(f"{title} {summary}")
    assert ai_labs._relevance_for(title, summary, "openai") == ai_labs._RELEVANCE_NON_WORK
    # 0.600 before, 0.460 now -- under every floor this deployment has run
    # (0.563 tonight, 0.590 on 08-31, 0.35 shipped default is not in play
    # because the calibrated floor supersedes it).
    assert _bid(title, summary, "openai") == 0.46


def test_bill_numbers_demote_across_chambers():
    for number in ("SB 1119", "AB 1064", "H.R. 9", "HB 22"):
        assert ai_labs._BILL_NUMBER_RE.search(f"A post about {number} today"), number


def test_bill_number_match_is_case_sensitive():
    """The capital is the discriminator -- the pattern above it runs
    IGNORECASE, where a bare lowercase 'sb 200' starts eating model tokens."""
    assert not ai_labs._BILL_NUMBER_RE.search("we shipped sb 200 last week")


def test_legislative_verb_and_chamber_grammars_demote():
    for title in (
        "Our position on the state bill on model evaluations",
        "A bill to require disclosure of training data",
        "Anthropic endorses the Senate bill on compute reporting",
        "OpenAI opposes California’s bill to ban open weights",
    ):
        assert ai_labs._is_non_work(title), title


def test_the_invoice_sense_of_bill_is_not_public_affairs():
    """The whole reason this is grammar and not `\\bbill\\b`. Pricing and
    billing posts are things a builder acts on; two of these are HIGH."""
    for title, summary in (
        ("Updated pricing for the OpenAI API",
         "Your monthly bill drops 40%. We now bill to the org, not the key."),
        ("New billing dashboard",
         "See your bill, set budgets, and export invoices."),
        ("Introducing usage-based billing",
         "Pay only for what you use; the bill is itemised per model."),
    ):
        assert not ai_labs._is_non_work(f"{title} {summary}"), title


def test_shipping_posts_keep_their_tier():
    """Guard the failure mode that matters more than the wake: a demotion
    that reaches launches, pricing or deprecations makes the mind deaf."""
    assert ai_labs._relevance_for("Introducing Claude 5 Opus", "", "anthropic") == ai_labs._RELEVANCE_HIGH
    assert ai_labs._relevance_for("Updated pricing for the OpenAI API", "", "openai") == ai_labs._RELEVANCE_HIGH
    assert ai_labs._relevance_for("Deprecating gpt-4o on the API", "", "openai") == ai_labs._RELEVANCE_HIGH
    # The documented must-stay-DEFAULT case from the existing audit suite.
    assert ai_labs._relevance_for(
        "AI governance for model deployment", "Best practices for governance.", "openai",
    ) == ai_labs._RELEVANCE_DEFAULT
