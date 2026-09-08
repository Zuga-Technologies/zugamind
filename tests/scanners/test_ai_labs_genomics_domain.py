"""Genomics is a field this mind already demotes -- on the one feed that says so.

Regression for the 2026-09-08 14:29Z wake: [deepmind] "AlphaGenome Atlas: A
predictive map of every possible DNA letter change in the human genome" scored
`_RELEVANCE_DEFAULT`, bid 0.600 against a 0.500 bar, and bought a Claude
session. It is the fourth instance of the class the module docstring's points 5
and 7 name -- AI applied to another scientific field.

What makes this one worth a fix rather than a shrug is that the judgement was
ALREADY recorded. The same subject, on the feed next door, is demoted today:

    [google_res] "Transfer learning for genomic prediction in
                  underrepresented populations"   -> 0.40, via "General Science"
    [deepmind]   "AlphaGenome Atlas: ... human genome"  -> 0.75, no rule fires

Two independent paths reach the same verdict in this scanner --
`_OFF_DOMAIN_CATEGORIES` (reads the feed's own taxonomy) and `_SCI_DOMAIN_RE`
(a phrase list, works on any feed). Path one already knows genomics. Path two
never learned the word, so the verdict was not portable off google_res.

`test_ai_labs_offdomain_category.py` warns, correctly, that reaching for more
vocabulary is the weak repeat fix and that reading the feed's own subject is
the structural one. That option was checked here before this file was written
and it does not exist for deepmind: a live fetch of deepmind/blog/rss.xml on
2026-09-08 returns items whose only children are title, link, description,
pubdate, guid, thumbnail and content -- ZERO <category> elements in the whole
feed (google_res: 283, openai: 1017, msft_research: 10 and all of them the
constant "Research Blog"). There is no taxonomy to read. On this feed the
phrase list is not the lazy lever, it is the only one.

Honest cost, so a later reader can weigh reverting: these phrases sit inside
`_is_non_work`, which is asked BEFORE the HIGH check, so a genuine launch whose
title or blurb says "human genome" goes quiet. That is why the clause is built
from phrases and not the field name -- exactly the discipline that lets
"Introducing WeatherNext 3, our most advanced and accurate global weather AI
model" still score HIGH while "weather forecast" demotes. A future "Introducing
AlphaGenome 3, our genomics model" promotes for the same reason; one that
advertises mapping the human genome in its own blurb does not.
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


_ALPHAGENOME_TITLE = (
    "AlphaGenome Atlas: A predictive map of every possible DNA letter change "
    "in the human genome"
)
_ALPHAGENOME_SUMMARY = (
    "AlphaGenome Atlas maps the molecular effects of 9 billion single-letter "
    "DNA variants across the human genome."
)


def test_the_wake_that_caused_this_no_longer_clears_the_floor():
    assert ai_labs._relevance_for(
        _ALPHAGENOME_TITLE, _ALPHAGENOME_SUMMARY, "deepmind",
    ) == ai_labs._RELEVANCE_NON_WORK
    # 0.600 before, 0.460 now -- under every floor this deployment has run.
    assert _bid(_ALPHAGENOME_TITLE, _ALPHAGENOME_SUMMARY, "deepmind") == 0.46


def test_the_title_alone_carries_the_subject():
    """The blurb is not load-bearing: deepmind truncates descriptions and a
    future variant of this post must not escape on a shorter summary."""
    assert ai_labs._is_non_work(_ALPHAGENOME_TITLE)


def test_the_summary_alone_carries_the_subject():
    assert ai_labs._is_non_work(_ALPHAGENOME_SUMMARY)


def test_the_category_path_cannot_reach_this_feed():
    """Proves the fix had to be vocabulary: deepmind is not a taxonomy feed and
    its live RSS publishes no <category> at all, so the google_res mechanism has
    nothing to read here."""
    assert "deepmind" not in ai_labs._TAXONOMY_LABS
    assert not ai_labs._category_is_off_domain(_ALPHAGENOME_SUMMARY)


def test_the_same_subject_already_demotes_on_the_feed_that_names_it():
    """The verdict this fix makes portable, asserted at its source."""
    assert ai_labs._relevance_for(
        "Transfer learning for genomic prediction in underrepresented populations",
        "General Science", "google_res",
    ) == ai_labs._RELEVANCE_NON_WORK


def test_the_other_live_item_in_the_same_blind_spot_demotes():
    """[msft_research] GigaPath sat at DEFAULT in the same sweep, bidding the
    identical 0.600. Its feed's only category is the constant "Research Blog",
    so it has no taxonomy to read either."""
    title = ("GigaPath-Flash and GigaTIME-Flash: Toward population-scale discovery "
             "with efficient pathology foundation models")
    summary = ("What if pathology foundation models could do more with less? "
               "GigaPath-Flash and GigaTIME-Flash cut computational demands while "
               "maintaining strong performance.")
    assert ai_labs._relevance_for(title, summary, "msft_research") == ai_labs._RELEVANCE_NON_WORK


def test_genomics_phrases_demote_on_any_feed():
    for text in (
        "Predicting gene expression from sequence with deep learning",
        "A genome-wide association study at scale",
        "Learning DNA methylation patterns across tissues",
        "A connectomics milestone: mapping a complete brain",
        "Foundation models for whole-slide images",
        "Advances in computational pathology",
    ):
        assert ai_labs._is_non_work(text), text


def test_a_product_launch_in_this_field_still_promotes():
    """The WeatherNext symmetry: the field name alone must not silence a ship.
    "Introducing WeatherNext 3 ... global weather AI model" scores HIGH today
    because the list carries "weather forecast", not "weather"."""
    assert ai_labs._relevance_for(
        "Introducing AlphaGenome 3, our most advanced genomics model",
        "Our newest model, now available to developers.", "deepmind",
    ) == ai_labs._RELEVANCE_HIGH


def test_the_gene_stem_does_not_swallow_ordinary_words():
    """"gene" is a prefix of generative/general/generate/generation, and this
    scanner's feeds are wall-to-wall generative AI."""
    for text in (
        "Introducing our next generative model",
        "General availability for the Batch API",
        "How we generate synthetic evaluation data",
        "A new generation of coding agents",
        "General Science",
    ):
        assert not ai_labs._SCI_DOMAIN_RE.search(text), text


def test_live_launches_from_the_same_sweep_are_untouched():
    """The nine HIGH-tier titles in the 2026-09-08 cache, asserted verbatim: a
    demotion rule that costs a launch is worse than the wake it prevents."""
    for title in (
        "Previewing the Model Hardware Standard",
        "Introducing Claude Opus 5",
        "Improving Fable 5's biology safeguards",
        "Introducing WeatherNext 3, our most advanced and accurate global weather AI model",
        "Introducing Gemini 3.8 Flash and 3.8 Flash Cyber",
        "Introducing agentic video understanding with Gemini",
        "Gemini Omni 1.1 Flash lets you build with more control",
        "Intelligent transcription with Gemini 3.5 Transcribe",
        "Introducing CARE-X: Towards Clinically Useful Radiology VLMs with Auxiliary "
        "Supervision, Reward-Aligned Decoding and Agentic Evaluation",
    ):
        assert ai_labs._relevance_for(title) == ai_labs._RELEVANCE_HIGH, title
