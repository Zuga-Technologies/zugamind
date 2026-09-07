"""A lab funding or running a social-impact program is public affairs.

Regression for the 2026-09-07 12:14Z wake: [openai] "Supporting independent
journalism in Ukraine" -- "OpenAI, AIRPPU and WAN-IFRA launch an AI program to
help Ukrainian news organizations strengthen innovation, resilience, and
independent journalism" -- scored DEFAULT 0.75, bid 0.600, and bought a Claude
session. Nothing in it changes what a builder can build or what it costs: it is
the lab paying for a cause, with NGOs as the partners and journalists as the
beneficiaries.

Public affairs is the tier this belongs to and the scanner has had it since
2026-08-18. The miss was vocabulary, again -- `_PUBLIC_AFFAIRS_RE` has named
"philanthrop" and nonprofits from the start, and the post used neither word.

Two things this file guards that are worth more than the wake:

1. The bare noun is never the match, same rule the bill and M&A grammars were
   written under. "grant" is also `GRANT SELECT`, an OAuth grant type, and
   "grant access to your org" -- and a demotion that reaches billing or
   permissions copy silences pricing posts, which are HIGH by definition.
   Only grammars the technical sense does not produce: a grant PROGRAM/FUND,
   a verb+grants pair, "$10M in grants", "grants for research".

2. `nonprofit` was in the pattern and matched almost nothing, because
   `\\bnonprofit\\b` cannot match "nonprofits" (the boundary fails on the
   plural) or "non-profit" (the hyphen). Both surface forms are what the
   posts actually use. That is a repair of an existing rule, not a new one.
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
    title = "Supporting independent journalism in Ukraine"
    summary = (
        "OpenAI, AIRPPU and WAN-IFRA launch an AI program to help Ukrainian "
        "news organizations strengthen innovation, resilience, and independent "
        "journalism."
    )
    assert ai_labs._is_non_work(f"{title} {summary}")
    assert ai_labs._relevance_for(title, summary, "openai") == ai_labs._RELEVANCE_NON_WORK
    # 0.600 before -- the bid the briefing quoted. 0.460 now, under every floor
    # this deployment has run (0.500 on the wake, 0.563 on 09-01, 0.590 on 08-31).
    assert _bid(title, summary, "openai") == 0.46


def test_grant_and_fund_programs_demote():
    """Every one of these sat at DEFAULT or HIGH on the live openai feed."""
    for title, summary in (
        ("Funding grants for new research into AI and mental health",
         "OpenAI is awarding up to $2 million in grants for research at the "
         "intersection of AI and mental health."),
        ("Superalignment Fast Grants",
         "We are launching $10M in grants to support technical research."),
        ("OpenAI Cybersecurity Grant Program",
         "Our goal is to facilitate AI-powered cybersecurity for defenders "
         "through grants and other support."),
        ("A People-First AI Fund: $50M to support nonprofits",
         "Applications are now open for OpenAI's People-First AI Fund."),
        ("Announcing the OpenAI Safety Fellowship",
         "A pilot program to support independent safety research."),
        ("EMEA Youth & Wellbeing Grant",
         "A EUR 500,000 program funding NGOs and researchers."),
    ):
        assert ai_labs._is_non_work(f"{title} {summary}"), title


def test_non_work_wins_over_the_launch_verb_in_these_titles():
    """"Announcing"/"Introducing" is HIGH vocabulary, and three of the posts
    in this class open with it. NON-WORK is checked first for exactly this
    reason -- the same precedence that stops a case study buying HIGH by
    naming the model it advertises."""
    for title, summary in (
        ("Announcing the OpenAI Safety Fellowship",
         "A pilot program to support independent safety and alignment research."),
        # Verbatim from the live openai feed. The adjective in "unrestricted
        # grants" is why the money grammar has an adjective slot.
        ("Announcing the initial People-First AI Fund grantees",
         "The OpenAI Foundation announces the initial recipients of the "
         "People-First AI Fund, awarding $40.5M in unrestricted grants to 208 "
         "nonprofits supporting community innovation and opportunity."),
        ("Introducing OpenAI Academy for News Organizations",
         "A new learning hub built with the American Journalism Project and "
         "The Lenfest Institute to help newsrooms use AI effectively."),
    ):
        assert ai_labs._HIGH_RELEVANCE_RE.search(title), f"premise: {title} is HIGH vocabulary"
        assert ai_labs._relevance_for(title, summary, "openai") == ai_labs._RELEVANCE_NON_WORK, title


def test_beneficiary_sectors_demote():
    """The sector nouns a lab uses when it is funding a cause. None has a
    builder reading -- the same standard "individual freedom" (point 6) and
    "safety institute(s)" (point 8) were added under, including the ones no
    post has used yet: they are the siblings of the one that just cost a
    session, and a word list that waits for each sibling to fire pays a wake
    per sector."""
    for title in (
        "Supporting independent journalism in Ukraine",
        "Defending press freedom in the age of AI",
        "Working with civil society on model deployment",
        "A human rights framework for frontier AI",
        "AI for humanitarian response",
        "Helping disaster response teams turn AI into action across Asia",
        "The Newsroom AI Catalyst: a global program with WAN-IFRA",
        "Grants for NGOs building with our models",
    ):
        assert ai_labs._is_non_work(title), title


def test_nonprofit_matches_the_plural_and_the_hyphen():
    """The repair. The old `\\bnonprofit\\b` matched neither form the posts
    actually use."""
    for title in (
        "A People-First AI Fund: $50M to support nonprofits",
        "Why OpenAI's structure must evolve to advance our mission: a "
        "stronger non-profit supported by the for-profit's success",
        "Our nonprofit commitments",
    ):
        assert ai_labs._is_non_work(title), title


def test_joint_program_with_a_named_org_demotes():
    """"a global program with WAN-IFRA" is the org-partnership class arriving
    through a door the "partners with" / "agreement with" list does not cover.
    Matched CASE-SENSITIVELY on the partner's proper noun -- the same
    discriminator the promo grammars use, and the reason a developer program
    with new API tiers is untouched."""
    assert ai_labs._ORG_PROGRAM_RE.search("a global program with WAN-IFRA")
    assert ai_labs._ORG_PROGRAM_RE.search(
        "a six-month pilot program with the Northern Ireland Education Authority")
    assert ai_labs._ORG_PROGRAM_RE.search("an initiative with Mozilla")
    # Lowercase tail == an ordinary noun phrase, not an org.
    assert not ai_labs._ORG_PROGRAM_RE.search("our partner program with improved rate limits")
    assert not ai_labs._ORG_PROGRAM_RE.search("the developer program with new API tiers")


def test_the_technical_senses_of_grant_are_not_public_affairs():
    """The whole reason this is grammar and not `\\bgrant\\b`. Permissions and
    billing copy must survive it -- pricing posts are HIGH."""
    for title, summary in (
        ("Introducing scoped API keys",
         "Grant access to your org, revoke a single key, and audit every grant."),
        ("Updated pricing for the OpenAI API",
         "Admins can grant seats per workspace."),
        ("Fine-grained authorization for the Assistants API",
         "Each grant type maps to one OAuth scope."),
    ):
        assert not ai_labs._is_non_work(f"{title} {summary}"), title


def test_shipping_posts_keep_their_tier():
    """The failure mode that matters more than the wake: a demotion that
    reaches launches, pricing or deprecations makes the mind deaf."""
    assert ai_labs._relevance_for("Introducing Claude 5 Opus", "", "anthropic") == ai_labs._RELEVANCE_HIGH
    assert ai_labs._relevance_for("Updated pricing for the OpenAI API", "", "openai") == ai_labs._RELEVANCE_HIGH
    assert ai_labs._relevance_for("Deprecating gpt-4o on the API", "", "openai") == ai_labs._RELEVANCE_HIGH
    assert ai_labs._relevance_for(
        "Introducing the OpenAI developer program with new API tiers", "", "openai",
    ) == ai_labs._RELEVANCE_HIGH
    # The documented must-stay-DEFAULT case from the existing audit suite.
    assert ai_labs._relevance_for(
        "AI governance for model deployment", "Best practices for governance.", "openai",
    ) == ai_labs._RELEVANCE_DEFAULT
