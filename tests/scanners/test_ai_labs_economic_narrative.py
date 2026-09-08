"""A lab's economic narrative is public affairs, and it is a content LINE.

Regression for the 2026-09-08 13:10Z wake: [openai] "The Work Now Within
Reach" -- "Explore how more capable, affordable AI can expand the work people
and businesses can accomplish -- and make growth more economical" -- scored
DEFAULT 0.75, bid 0.600 against a 0.500 bar, and bought a Claude session. It
names no model, no price and no endpoint. It is an essay about AI and the
economy.

The tier is not new. `_PUBLIC_AFFAIRS_RE` has carried "economic index" and
"economic research" since 2026-08-18. The miss is that those are two SURFACE
FORMS of a standing content line, not the line: OpenAI ships Economic
Blueprints per country, an economic-opportunity/Jobs Platform program,
economic-impact studies, and essays that carry them. The list was learning
that line one headline at a time, at one session per headline.

Two things this file guards that are worth more than the wake:

1. The bare stem is never the match, same rule the grant, bill and M&A
   grammars were written under -- and here it is a close call worth
   recording, because `economic*` alone measures at 17 demotions with zero
   HIGH collateral and looks fine. It is not: two of the 17 are real work
   demoted on a passing mention (an economics ANALOGY inside a reward-hacking
   research post, and a cloud-cost caching paper). The match is economics
   paired with a policy/program noun, or growth and economics inside one
   clause.

2. NON-WORK is checked before HIGH (point 2), and this class needs it: the
   Jobs Platform post opens "OpenAI is launching...". A model token or a
   launch verb does not buy a narrative post the launch tier.
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
    title = "The Work Now Within Reach"
    summary = (
        "Explore how more capable, affordable AI can expand the work people "
        "and businesses can accomplish—and make growth more economical."
    )
    assert ai_labs._is_non_work(f"{title} {summary}")
    assert ai_labs._relevance_for(title, summary, "openai") == ai_labs._RELEVANCE_NON_WORK
    # 0.600 before -- the bid the briefing quoted. 0.460 now, under every
    # floor this deployment has run (0.500 on the wake, 0.563 on 09-01).
    assert _bid(title, summary, "openai") == 0.46


def test_the_economic_content_line_demotes():
    """Every one of these sat at DEFAULT on the live openai feed, i.e. above
    the wake floor, waiting its turn to buy a session."""
    for title, summary in (
        ("AI in Japan—OpenAI’s Japan Economic Blueprint",
         "OpenAI’s Japan Economic Blueprint outlines how Japan can harness AI "
         "to boost innovation and strengthen competitiveness."),
        ("AI in South Korea—OpenAI’s Economic Blueprint",
         "OpenAI's Korea Economic Blueprint outlines how South Korea can scale "
         "trusted AI through sovereign capabilities."),
        ("The next chapter for AI in the EU",
         "OpenAI launches the EU Economic Blueprint 2.0 with new data, "
         "partnerships, and initiatives to accelerate AI adoption."),
        ("Expanding economic opportunity with AI",
         "OpenAI is launching a Jobs Platform and new Certifications to connect "
         "workers with jobs, training, and certification."),
        ("OpenAI’s new economic analysis",
         "Analysis provides insights into ChatGPT’s impact on the economy."),
        ("Economic impacts research at OpenAI",
         "Call for expressions of interest to study the economic impacts of "
         "large language models."),
        ("A research agenda for assessing the economic impacts of code "
         "generation models", ""),
        ("Seizing the AI opportunity",
         "Meeting the demands of the Intelligence Age will require strategic "
         "investment in energy and infrastructure to drive economic growth."),
    ):
        assert ai_labs._is_non_work(f"{title} {summary}"), title


def test_growth_and_economics_in_one_clause_is_a_macro_claim():
    """The wake's own phrasing names no program and no field. Either order,
    and only within a clause -- a period ends the window, so two unrelated
    sentences do not pair."""
    assert ai_labs._is_non_work("make growth more economical")
    assert ai_labs._is_non_work("economic policies that unlock growth")
    assert not ai_labs._is_non_work(
        "Faster growth in tokens per second. Our economical new tier ships today.")


def test_non_work_wins_over_the_launch_verb_in_these_titles():
    """The Jobs Platform post opens with a launch verb. NON-WORK is checked
    first for exactly this reason -- the same precedence that stops a case
    study buying HIGH by naming the model it advertises."""
    title = "Expanding economic opportunity with AI"
    summary = ("OpenAI is launching a Jobs Platform and new Certifications to "
               "connect workers with jobs and training.")
    assert ai_labs._HIGH_RELEVANCE_RE.search(summary), "premise: launch vocabulary present"
    assert ai_labs._relevance_for(title, summary, "openai") == ai_labs._RELEVANCE_NON_WORK


def test_a_passing_mention_of_economics_is_not_the_economic_line():
    """The whole reason this is grammar and not `economic*`. Measured, the
    bare stem demotes all three of these; two are real work and the third is
    an eval a builder can run against."""
    for title, summary in (
        # An economics ANALOGY inside a reward-hacking research post.
        ("Measuring Goodhart’s law",
         "Goodhart’s law famously says: “When a measure becomes a target, it "
         "ceases to be a good measure.” Although originally from economics, it "
         "is something we grapple with at OpenAI."),
        # Cost engineering, which is exactly what a builder acts on.
        ("Optimizing cloud economics with linear elastic caching",
         "Algorithms & Theory"),
        # GDPval. An arguable keep, and deliberately kept.
        ("Measuring the performance of our models on real-world tasks",
         "OpenAI introduces GDPval, a new evaluation that measures model "
         "performance on real-world economically valuable tasks."),
    ):
        assert not ai_labs._is_non_work(f"{title} {summary}"), title


def test_shipping_posts_keep_their_tier():
    """The failure mode that matters more than the wake: a demotion that
    reaches launches, pricing or deprecations makes the mind deaf. Measured
    over 1384 live items, this rule's HIGH collateral is zero -- these are
    the named guards for the words it came closest to."""
    for title, summary in (
        ("Advancing the price-performance frontier with GPT-5.6",
         "Explore lower GPT‑5.6 pricing for Luna and Terra—and how OpenAI’s "
         "more efficient models help enterprises deploy AI workflows."),
        ("Updated pricing for the OpenAI API",
         "More economical rates across the board, effective today."),
        ("Introducing Claude 5 Opus", ""),
        ("Deprecating gpt-4o on the API", ""),
    ):
        assert ai_labs._relevance_for(title, summary, "openai") == ai_labs._RELEVANCE_HIGH, title


def test_the_two_surface_forms_this_rule_replaced_still_match():
    """"economic index" and "economic research" were the old vocabulary. The
    grammar that replaced them must not drop what they caught."""
    assert ai_labs._is_non_work("The OpenAI Economic Index")
    assert ai_labs._is_non_work("Economic research at OpenAI")
