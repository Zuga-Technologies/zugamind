"""A research feed that publishes its own subject should be read, not guessed.

Regression for the 2026-09-01 18:51Z wake: [google_res] "Mapping global
methane emissions from space with deep learning" bid 0.600 against a 0.590
floor and bought a Claude session. It is the third instance of the class the
module docstring's points 5 and 7 already named -- AI applied to another
scientific field -- and the second time the fix for it was reached for as more
vocabulary. `_SCI_DOMAIN_RE` already carries "climate model" and "climate
change"; the headline says neither.

What makes google_res different is that its RSS summary is not prose at all,
it is a label from Google's research-areas taxonomy ("Climate &
Sustainability"). So the subject never had to be guessed.

The other half of these tests is the ordering invariant, which is the one way
this fix could be worse than the wake it prevents. Every other NON-WORK rule
in this scanner is asked BEFORE the HIGH check; this one is asked after,
because a category says which shelf a post sits on and a lab can ship a real
tool from any shelf. "Introducing Groundsource" is filed under Climate &
Sustainability on the live feed and is a genuine launch.
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
    title = "Mapping global methane emissions from space with deep learning"
    summary = "Climate & Sustainability"
    assert ai_labs._relevance_for(title, summary, "google_res") == ai_labs._RELEVANCE_NON_WORK
    # 0.600 before, 0.460 now -- under every floor this deployment has run.
    assert _bid(title, summary, "google_res") == 0.46


def test_the_vocabulary_rules_alone_would_still_miss_it():
    """Proves the fix had to be the tier and not another phrase: with the
    category set emptied, the existing subject regexes do not catch this."""
    title = "Mapping global methane emissions from space with deep learning"
    assert not ai_labs._is_non_work(f"{title} Climate & Sustainability")


def test_every_off_domain_category_demotes():
    for category in sorted(ai_labs._OFF_DOMAIN_CATEGORIES):
        assert ai_labs._relevance_for(
            "A post whose title says nothing recognisable", category, "google_res",
        ) == ai_labs._RELEVANCE_NON_WORK, category


def test_a_launch_filed_under_an_off_domain_category_keeps_high():
    """The ordering invariant. Measured on the live feed 2026-09-01: this is a
    real post, filed under Climate & Sustainability, and it ships a tool."""
    assert ai_labs._relevance_for(
        "Introducing Groundsource: Turning news reports into data with Gemini",
        "Climate & Sustainability", "google_res",
    ) == ai_labs._RELEVANCE_HIGH
    assert ai_labs._relevance_for(
        "Now available: a health research API", "Health & Bioscience", "google_res",
    ) == ai_labs._RELEVANCE_HIGH


def test_builder_categories_stay_default():
    """64 of the live window's 100 items. A demotion that reached these would
    make the feed deaf, which is strictly worse than the wake it prevents."""
    for category in (
        "Generative AI", "Algorithms & Theory", "Machine Intelligence",
        "Natural Language Processing", "Data Management", "Machine Perception",
        "Security, Privacy and Abuse Prevention",
        "Human-Computer Interaction and Visualization",
    ):
        assert ai_labs._relevance_for(
            "A post whose title says nothing recognisable", category, "google_res",
        ) == ai_labs._RELEVANCE_DEFAULT, category


def test_education_innovation_is_deliberately_not_demoted():
    """Documented exclusion, so adding it later is a deliberate act with a
    test to change. One of its three live items is "Testing LLMs on
    superconductivity research questions" -- a capability eval, which is
    builder evidence wearing a domain label."""
    assert ai_labs._relevance_for(
        "Testing LLMs on superconductivity research questions",
        "Education Innovation", "google_res",
    ) == ai_labs._RELEVANCE_DEFAULT


def test_the_rule_is_scoped_to_the_taxonomy_feed():
    """Only google_res publishes a category here. On every other feed the
    summary is prose, and a post that happens to discuss general science in
    its blurb must not be demoted by a substring of it."""
    for lab in ("openai", "anthropic", "deepmind", "anthropic_res", "msft_research"):
        assert ai_labs._relevance_for(
            "A post whose title says nothing recognisable", "General Science", lab,
        ) == ai_labs._RELEVANCE_DEFAULT, lab


def test_the_label_is_matched_on_normalised_text():
    """It is CMS copy, not an identifier -- nothing guarantees the casing or
    the spacing survives an edit on Google's side."""
    for variant in ("climate & sustainability", "  Climate &  Sustainability  ",
                    "CLIMATE & SUSTAINABILITY", "Climate &\nSustainability"):
        assert ai_labs._relevance_for(
            "A post whose title says nothing recognisable", variant, "google_res",
        ) == ai_labs._RELEVANCE_NON_WORK, repr(variant)


def test_an_unlabelled_call_keeps_the_old_answer():
    """`lab` defaults to "" for callers that do not pass it; the category rule
    must not fire on that path."""
    assert ai_labs._relevance_for(
        "A post whose title says nothing recognisable", "Climate & Sustainability",
    ) == ai_labs._RELEVANCE_DEFAULT
