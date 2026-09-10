"""Antimicrobial drug discovery is a field this mind already demotes -- under
the name it happened to learn first.

Regression for the 2026-09-10 16:24Z wake: [openai] "How a researcher uses
Codex and ChatGPT to search for new antimicrobial molecules" -- summary "Cesar
de la Fuente's lab uses Codex and ChatGPT to search living and extinct genomes
for antimicrobial candidates to fight drug-resistant infections" -- scored
0.75, bid 0.600 against a 0.500 bar, and bought a Claude session. It is the
seventh wake of this class and the second inside pharmacology specifically.

What makes this a fix and not a shrug is the same thing that made
`test_ai_labs_genomics_domain.py` a fix two days earlier: the verdict was
ALREADY recorded, under different words. `_SCI_DOMAIN_RE` has matched
`drug discovery` and `drug design` since it was written. This post is drug
discovery -- a wet-lab pharmacology group screening genomes for antibiotic
candidates -- and it says so in plain language. It just never uses either
phrase:

    "drug discovery"        -> not present
    "drug design"           -> not present
    "drug-resistant"        -> present, and the old clause required a SPACE
                               after "drug", so a hyphen could never match
    "antimicrobial"         -> present twice, unknown word
    "living and extinct genomes" -> present; `human genome` / `genome-wide` /
                                    `genomic sequencing` all miss a bare plural

So the gap is not judgement, it is spelling. Three ways in to one subject the
scanner had already ruled off-domain, and it knew exactly one of them.

The trap, and why the obvious lever is the wrong one: a bare `genomes?` would
close this instance in one token, and it is exactly what the genomics
docstring warns against. These phrases live inside `_is_non_work`, which is
asked BEFORE the HIGH check, so a demotion beats a launch. A real
"Introducing AlphaGenome 3, our genomics model" whose blurb mentioned genomes
would go silent. The construction "living and extinct genomes" is the
phrase-level tell instead -- archival-corpus vocabulary that no shipping post
uses -- on the same discipline that lets "Introducing WeatherNext 3" survive
"weather forecast".

`antimicrobial` / `antibacterial` / `antibiotic` are taken bare because,
unlike "evaluation" or "contract" in the docstring's points 8 and 9, they have
no technical second reading a lab could ship under. There is no antimicrobial
API.

Honest cost, so a later reader can weigh reverting: if a lab ever ships a
genuine developer tool whose one-line pitch is antibiotic screening -- an
"Introducing FoldRx, our antimicrobial design model" -- this rule silences it,
because NON-WORK is asked first. That is the same bet points 5 and 7 already
took for weather and protein folding, and it has been the right side of the
trade six wakes running.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "zugamind") not in sys.path:
    sys.path.insert(0, str(_ROOT / "zugamind"))

from zugamind.scanners.world import ai_labs  # noqa: E402

_BAR = 0.500


def _bid(title: str, summary: str = "", lab: str = "", urgency: float = 0.25) -> float:
    """WorldSignals' price for a trigger, so the assertions are in the units
    that actually decide whether a session gets bought."""
    return round(0.25 + 0.4 * ai_labs._relevance_for(title, summary, lab) + 0.2 * urgency, 4)


_WAKE_TITLE = (
    "How a researcher uses Codex and ChatGPT to search for new antimicrobial molecules"
)
_WAKE_SUMMARY = (
    "Cesar de la Fuente's lab uses Codex and ChatGPT to search living and extinct "
    "genomes for antimicrobial candidates to fight drug-resistant infections."
)


def test_the_wake_that_caused_this_no_longer_clears_the_floor():
    """The exact item, at the exact urgency it arrived with."""
    assert _bid(_WAKE_TITLE, _WAKE_SUMMARY, "openai") < _BAR


def test_relevance_lands_on_the_off_domain_tier_not_default():
    """0.75 was the no-rule-fires default. It should now be demoted."""
    assert ai_labs._relevance_for(_WAKE_TITLE, _WAKE_SUMMARY, "openai") <= 0.40


def test_the_title_alone_is_enough():
    """openai's RSS carries the subject in the description, but this title says
    "antimicrobial molecules" unaided -- a feed that drops the summary must
    still demote."""
    assert ai_labs._relevance_for(_WAKE_TITLE, "", "openai") <= 0.40


def test_each_of_the_three_spellings_demotes_on_its_own():
    """One subject, three doors. Each must close, so the next sibling post
    phrasing it differently does not buy another session."""
    for phrase in (
        "a new antimicrobial peptide found by an LLM",
        "antibacterial candidates from a protein language model",
        "screening for new antibiotics with generative models",
        "fighting drug-resistant infections with AI",
        "mining living and extinct genomes for molecules",
    ):
        assert ai_labs._SCI_DOMAIN_RE.search(phrase), phrase


def test_the_already_recorded_verdict_still_holds():
    """The phrase the scanner did know. Guards against a refactor that
    replaces the old clause instead of widening it."""
    assert ai_labs._SCI_DOMAIN_RE.search("AI for drug discovery")
    assert ai_labs._SCI_DOMAIN_RE.search("generative drug design")


def test_the_genomics_wake_this_builds_on_is_untouched():
    """2026-09-08's AlphaGenome item must keep its demotion."""
    alphagenome = (
        "AlphaGenome Atlas: A predictive map of every possible DNA letter change "
        "in the human genome"
    )
    assert ai_labs._relevance_for(alphagenome, "", "deepmind") <= 0.40


def test_a_bare_genome_mention_does_not_silence_a_launch():
    """The lever deliberately NOT pulled. If someone widens this to a bare
    `genomes?`, this test is the one that should go red."""
    assert not ai_labs._SCI_DOMAIN_RE.search(
        "Introducing AlphaGenome 3, our most capable genomics model"
    )
    assert not ai_labs._SCI_DOMAIN_RE.search("a model trained on genomes")


def test_real_launches_keep_their_relevance():
    """The whole point of phrase-level demotion: shipping still gets through."""
    for title in (
        "Introducing GPT-5.6 Sol",
        "Introducing WeatherNext 3, our most advanced global weather AI model",
        "Deprecating the completions API",
    ):
        assert ai_labs._relevance_for(title, "", "openai") > 0.40, title
