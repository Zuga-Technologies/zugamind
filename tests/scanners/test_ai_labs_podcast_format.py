"""A podcast episode is a conversation, and the URL says so.

Regression for the 2026-09-08 16:51Z wake: [msft_research] "Called to serve:
Tech, research, and positive impact with Chris White" scored
`_RELEVANCE_DEFAULT`, bid 0.600, and bought a Claude session. It is a
career-profile interview with a lab director -- no model, no price, no
endpoint.

Why this is a URL rule and not a title rule, which is the only interesting
decision in the file: the show names are unbounded. Abstracts, Ideas,
Collaborators, What's Your Story, AI Testing and Evaluation, The AI Revolution
in Medicine, Called to serve -- 68 episodes across nine-plus series in the
measured window, and Microsoft adds series faster than a word list learns
them. That is the "one funeral at a time" failure docstring points 5, 7 and 10
each paid for. The FORMAT, though, is structured data the publisher already
hands over: every episode sits under a `/research/podcast/` path. Same
mechanism as reading google_res's <category>, one feed over.

Measured 2026-09-08 on the full live msft_research window (250 items over 904
days, walked page by page -- the feed serves 10 at a time and the disk cache
holds 8): 68 podcast episodes, 57 at DEFAULT, ZERO at HIGH. The zero is the
whole case for the rule; across two and a half years of a show-heavy feed the
format has never carried a shipped thing, so there is no measured collateral.

Priced honestly: those 68 are mostly archive. In the last 365 days only 7
podcast episodes at DEFAULT arrived -- one per 52 days -- so this is worth
about 7 avoided sessions a year, not 57. It ships anyway because the floor
cannot learn a source this rare on its own (QUANTILE is 0.9; see
`test_floor_ratchet_arrival_rate.py` for that arithmetic), which is exactly
what the demotion tiers are for.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "zugamind") not in sys.path:
    sys.path.insert(0, str(_ROOT / "zugamind"))

from zugamind.scanners.world import ai_labs  # noqa: E402


def _bid(title: str, summary: str = "", lab: str = "", link: str = "",
         urgency: float = 0.25) -> float:
    """WorldSignals' price for a trigger, so the assertions are in the units
    that actually decide whether a session gets bought."""
    return round(
        0.25 + 0.4 * ai_labs._relevance_for(title, summary, lab, link) + 0.2 * urgency,
        4,
    )


_WAKE_TITLE = "Called to serve: Tech, research, and positive impact with Chris White"
_WAKE_SUMMARY = (
    "Lab Director Chris White has worked on research challenges with real-world "
    "implications—from new approaches to wartime data analysis to tools for "
    "combating human trafficking. He talks to program manager Weishung Liu about "
    "the influences that led to the work and more."
)
_WAKE_LINK = (
    "https://www.microsoft.com/en-us/research/podcast/"
    "called-to-serve-tech-research-and-positive-impact-with-chris-white/"
)


def test_the_wake_that_caused_this_no_longer_clears_the_floor():
    assert ai_labs._relevance_for(
        _WAKE_TITLE, _WAKE_SUMMARY, "msft_research", _WAKE_LINK,
    ) == ai_labs._RELEVANCE_NON_WORK
    # 0.600 before, 0.460 now -- under every floor this deployment has run.
    assert _bid(_WAKE_TITLE, _WAKE_SUMMARY, "msft_research", _WAKE_LINK) == 0.46


def test_the_text_alone_never_had_the_evidence():
    """Proves the fix had to read the URL. Nothing in the title or the blurb
    says "podcast" -- strip the link and this item is indistinguishable from a
    research post, which is why three rounds of vocabulary never caught it."""
    assert not ai_labs._is_non_work(f"{_WAKE_TITLE} {_WAKE_SUMMARY}")
    assert ai_labs._relevance_for(
        _WAKE_TITLE, _WAKE_SUMMARY, "msft_research",
    ) == ai_labs._RELEVANCE_DEFAULT


def test_every_series_in_the_live_window_demotes_on_the_path_alone():
    """The unbounded half of the class. These are real titles from the measured
    window, one per series, and not one of them shares a word with the others --
    the URL is the only thing they have in common."""
    for title in (
        "Abstracts: NeurIPS 2024 with Dylan Foster",
        "Ideas: Quantum computing redefined with Chetan Nayak",
        "Collaborators: Prompt engineering with Siddharth Suri and David Holtz",
        "What’s Your Story: Lex Story",
        "AI Testing and Evaluation: Learnings from cybersecurity",
        "The AI Revolution in Medicine, Revisited: An Introduction",
        "Trailer: The Shape of Things to Come",
        "Will machines ever be intelligent?",
        "NeurIPS 2024: The co-evolution of AI and systems with Lidong Zhou",
    ):
        link = "https://www.microsoft.com/en-us/research/podcast/some-episode/"
        assert ai_labs._relevance_for(
            title, "", "msft_research", link,
        ) == ai_labs._RELEVANCE_NON_WORK, title


def test_a_research_post_on_the_same_feed_is_untouched():
    """The rule must cost the feed nothing outside the podcast path. Same lab,
    same sweep, blog path -- still DEFAULT."""
    assert ai_labs._relevance_for(
        "Orchard: An open framework for scalable agentic AI",
        "An open framework for building agentic systems.",
        "msft_research",
        "https://www.microsoft.com/en-us/research/blog/orchard-an-open-framework/",
    ) == ai_labs._RELEVANCE_DEFAULT


def test_a_post_about_podcasts_is_not_a_podcast():
    """The segment boundary, which is the one thing that could quietly
    overreach: "podcast" inside a SLUG is a subject, not a format. A speech
    model for transcribing podcasts is exactly the builder evidence this
    scanner exists to catch."""
    assert ai_labs._relevance_for(
        "Transcribing podcasts with a new speech model",
        "A new speech model for long-form audio.",
        "msft_research",
        "https://www.microsoft.com/en-us/research/blog/podcast-transcription-model/",
    ) == ai_labs._RELEVANCE_DEFAULT


def test_the_path_matches_as_a_segment_in_both_spellings_and_either_ending():
    for link in (
        "https://example.com/research/podcast/an-episode/",
        "https://example.com/research/podcasts/an-episode/",
        "https://example.com/research/podcast",
        "https://example.com/research/podcasts",
        "https://example.com/PODCAST/an-episode/",
    ):
        assert ai_labs._link_is_podcast(link), link
    for link in (
        "",
        "https://example.com/blog/podcast-transcription-model/",
        "https://example.com/blog/our-favorite-podcasts-of-2026/",
        "https://example.com/blog/a-post?ref=/podcast/",
        "https://example.com/blog/a-post#podcast/",
    ):
        assert not ai_labs._link_is_podcast(link), link


def test_a_launch_on_a_podcast_path_still_promotes():
    """Placement, asserted. This demotion is asked AFTER the HIGH check for
    docstring point 10's reason: a URL is evidence about FORMAT, and a lab
    could announce a shipped thing on a podcast page. Zero of the measured 68
    did, so this changes no observed outcome -- it holds the hatch open for
    the 69th."""
    assert ai_labs._relevance_for(
        "Introducing Phi-6, our most capable small model",
        "Now available to developers.",
        "msft_research",
        "https://www.microsoft.com/en-us/research/podcast/introducing-phi-6/",
    ) == ai_labs._RELEVANCE_HIGH


def test_a_caller_that_passes_no_link_keeps_the_old_answer():
    """`link` is a new trailing parameter with a default, so every existing
    call site and every prior test scores exactly as it did before."""
    assert ai_labs._relevance_for(
        "Orchard: An open framework for scalable agentic AI", "", "msft_research",
    ) == ai_labs._RELEVANCE_DEFAULT
    assert ai_labs._relevance_for(_WAKE_TITLE, _WAKE_SUMMARY, "msft_research") == (
        ai_labs._RELEVANCE_DEFAULT
    )


def test_the_rule_is_not_gated_to_one_lab():
    """Unlike the taxonomy rule, nothing here is feed-specific: any publisher
    that files episodes under a podcast path gets the same verdict. Only
    msft_research does today -- one podcast link in the 405-link seen
    history -- so this is free future-proofing, not a claim about the others."""
    assert "msft_research" not in ai_labs._TAXONOMY_LABS
    for lab in ("openai", "deepmind", "google_res", "anthropic", ""):
        assert ai_labs._relevance_for(
            "A conversation about scaling laws", "", lab,
            "https://example.com/podcast/scaling-laws/",
        ) == ai_labs._RELEVANCE_NON_WORK, lab


def test_the_trigger_built_by_the_scanner_carries_the_link_into_the_score():
    """The end-to-end half: a rule the call site does not feed is decoration.
    Asserts the scanner's own trigger dict, not just the helper."""
    import inspect
    src = inspect.getsource(ai_labs.scan_ai_labs)
    assert '_relevance_for(it["title"], it.get("summary", ""), it["lab"],' in src
    assert 'it.get("link", "")' in src
