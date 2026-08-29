"""Tests for the self-modification PROPOSER -- the part of the lane that was
still missing after the socket landed: something in the loop that composes
a change to one of the agent's own cognition files.

The smallest honest proposer: a SELF-domain reflection that a real source
answered is shown to the local model, which says NONE or ONE standing line;
that line is appended to the SENTINEL's runtime override through
self_mod.propose -- so the cooldown, the audit log, and the apply flag are
all inherited, not re-implemented. Dark behind
ZUGAMIND_SELF_MOD_PROPOSER_ENABLED.
"""
from __future__ import annotations

import pytest

from cognition import proposer, self_mod
from continuity import journal
from foundation import identity
from gates.self_mod_cooldown import SelfModCooldown


def _events(kind):
    return [e for e in journal.read_events(limit=500) if e.get("kind") == kind]


def _reflection(domain="SELF", answered=True, question="why do I re-check the api port every cycle?",
                answer="stream/runner.py:301 probes it unconditionally"):
    return {"domain": domain, "question": question, "answered": answered,
            "answer": answer, "source": "code_search", "confidence": 0.7}


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("ZUGAMIND_SELF_MOD_PROPOSER_ENABLED", "true")


@pytest.fixture
def model_says(monkeypatch):
    def _set(reply):
        monkeypatch.setattr(proposer, "ollama_query", lambda *a, **kw: reply)
    return _set


@pytest.fixture
def model_must_not_run(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("the local model must not be consulted here")
    monkeypatch.setattr(proposer, "ollama_query", _boom)


# ---------------------------------------------------------------------------
# the happy path goes through self_mod, so cooldown/audit/flag are inherited
# ---------------------------------------------------------------------------

def test_a_self_reflection_with_a_lesson_becomes_a_proposal_on_the_sentinel_override(on, model_says):
    model_says("Probe a port only when something depends on the answer.")

    out = proposer.propose_from_reflection(_reflection())

    assert out["proposed"] is True
    assert out["line"] == "Probe a port only when something depends on the answer."
    proposed = _events("cognition_mod_proposed")  # SELF_MOD flag is off: recorded, not applied
    assert len(proposed) == 1 and proposed[0]["facet"] == "sentinel"
    assert SelfModCooldown().is_cooling(str(identity.override_path("sentinel"))) is True


def test_the_line_is_appended_to_an_existing_override_not_a_replacement(on, model_says, monkeypatch):
    monkeypatch.setenv("ZUGAMIND_SELF_MOD_ENABLED", "true")
    path = identity.override_path("sentinel")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Keep it short.", encoding="utf-8")
    model_says("Probe a port only when something depends on the answer.")

    proposer.propose_from_reflection(_reflection(), cooldown=SelfModCooldown(cooldown_hours=0.0))

    assert path.read_text(encoding="utf-8") == (
        "Keep it short.\nProbe a port only when something depends on the answer.")


# ---------------------------------------------------------------------------
# every non-proposal is a journaled skip with a reason, never a silent nothing
# ---------------------------------------------------------------------------

def test_model_says_none_means_no_proposal(on, model_says):
    model_says("NONE")
    out = proposer.propose_from_reflection(_reflection())
    assert out["proposed"] is False and out["reason"] == "none"
    assert _events("self_mod_proposal_skipped")[-1]["reason"] == "none"
    assert _events("cognition_mod_proposed") == []


def test_only_answered_self_reflections_are_candidates(on, model_must_not_run):
    assert proposer.propose_from_reflection(_reflection(domain="OPERATIONAL"))["reason"] == "not_a_candidate"
    assert proposer.propose_from_reflection(_reflection(answered=False))["reason"] == "not_a_candidate"
    assert _events("self_mod_proposal_skipped") == []  # not candidates -> not even a skip


def test_a_duplicate_line_is_not_proposed_and_does_not_cool(on, model_says):
    path = identity.override_path("sentinel")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Probe a port only when something depends on the answer.", encoding="utf-8")
    model_says("Probe a port only when something depends on the answer.")

    out = proposer.propose_from_reflection(_reflection())

    assert out["reason"] == "duplicate"
    assert SelfModCooldown().is_cooling(str(path)) is False


def test_a_full_override_is_not_grown(on, model_must_not_run):
    path = identity.override_path("sentinel")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x" * proposer.MAX_OVERRIDE_CHARS, encoding="utf-8")

    out = proposer.propose_from_reflection(_reflection())

    assert out["reason"] == "override_full"
    assert _events("self_mod_proposal_skipped")[-1]["reason"] == "override_full"


def test_dark_by_default_never_consults_the_model(monkeypatch, model_must_not_run):
    monkeypatch.delenv("ZUGAMIND_SELF_MOD_PROPOSER_ENABLED", raising=False)
    out = proposer.propose_from_reflection(_reflection())
    assert out["reason"] == "disabled"
    assert _events("self_mod_proposal_skipped")[-1]["reason"] == "disabled"


def test_an_unavailable_model_is_a_skip_not_a_crash(on, model_says):
    model_says(None)
    out = proposer.propose_from_reflection(_reflection())
    assert out["proposed"] is False and out["reason"] == "model_unavailable"


def test_a_rambling_model_reply_is_cut_to_one_line_under_the_cap(on, model_says):
    model_says("Probe a port only when something depends on the answer.\nAlso, more thoughts here.")
    out = proposer.propose_from_reflection(_reflection())
    assert out["line"] == "Probe a port only when something depends on the answer."
