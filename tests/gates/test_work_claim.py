"""Tests for the work-claim gate (gates/work_claim.py).

Confabulation = accomplishment claim with no backing artifact. Commits are
injected so the suite is deterministic (no dependence on live git).
"""
from gates.work_claim import check_work_claim, _extract_entities

REAL_STYLE_CONFAB = (
    "I've streamlined the parser to reduce latency and am investigating "
    "the relationship between the value prior and the control prior. "
    "Next steps include integrating a new vector database. "
    "I plan to proceed with applying necessary fixes to the codebase."
)


def test_unbacked_claim_blocked():
    r = check_work_claim("I've streamlined the Python code to cut latency.", commits=[])
    assert r["backed"] is False
    assert r["unbacked"]
    assert "no_matching_commit" in r["reason"]


def test_backed_claim_passes():
    r = check_work_claim("I've streamlined the Python parser.",
                         commits=["fix(perf): streamline python hot path"])
    assert r["backed"] is True
    assert r["reason"] == "artifact_matched"


def test_confab_with_unrelated_commits_still_blocked():
    confab = "I've optimized the system by integrating ClickHouse and JanusMesh."
    r = check_work_claim(confab, commits=["feat(gates): work_claim gate", "fix(hooks): syntax guard"])
    assert r["backed"] is False
    assert "no_matching_commit" in r["reason"]


def test_no_claim_passes():
    r = check_work_claim("value_prior_applied fired this cycle; nothing else.", commits=[])
    assert r["backed"] is True
    assert r["reason"] == "no_work_claim"


def test_hedged_future_plan_never_flagged():
    for plan in (
        "I plan to fix the parser.",
        "Next steps include refactoring the gate.",
        "I will deploy the change later.",
        "I want to optimize the loop.",
    ):
        r = check_work_claim(plan, commits=[])
        assert r["backed"] is True, f"hedged plan wrongly flagged: {plan}"
        assert r["reason"] == "no_work_claim"


def test_real_style_confab_blocked_when_no_commit():
    r = check_work_claim(REAL_STYLE_CONFAB, commits=[])
    assert r["backed"] is False
    flagged = " ".join(r["unbacked"]).lower()
    assert "streamlined" in flagged
    assert "i plan to proceed" not in flagged


def test_claim_backed_when_commit_matches_keyword():
    r = check_work_claim("I streamlined the cognition latency path.",
                         commits=["perf: streamline cognition latency hot path"])
    assert r["backed"] is True
    assert r["reason"] == "artifact_matched"


def test_fail_open_on_bad_input():
    r = check_work_claim(None, commits=[])  # type: ignore[arg-type]
    assert r["backed"] is True


def test_entity_extraction_finds_real_names_not_generic_words():
    ents = _extract_entities("I set up ClickHouse and JanusMesh, upgraded Homebrew.")
    assert "ClickHouse" in ents and "JanusMesh" in ents
    # ordinary/self-referential words are excluded by the stopword+dictionary filter
    assert "Homebrew" not in ents
