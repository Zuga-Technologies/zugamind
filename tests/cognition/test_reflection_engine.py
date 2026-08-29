"""Tests for the idle-cycle reflection engine (issue #4 wiring, 2026-08-29).

REFLECTING was a state string with nothing behind it. These tests are about
the "behind it" part: that an idle mind picks a real subject, refuses a stale
one, grounds its question in live state, and reads back the drift series the
floor calibrator has been writing all along.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from cognition.reflection import engine
from cognition.reflection.question_generator import generate_question
from continuity import journal


def _cycle(winner):
    journal.append_event("cycle", {"trigger_count": 1, "winner": winner})


def _events(kind):
    return [e for e in journal.read_events(limit=500) if e.get("kind") == kind]


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("ZUGAMIND_REFLECTION_ENABLED", "true")


# ---------------------------------------------------------------------------
# subject selection
# ---------------------------------------------------------------------------

def test_reflects_on_the_most_recent_real_winner():
    _cycle({"source_module": "old", "content": "an older winner"})
    _cycle(None)  # idle cycles in between must not become the subject
    _cycle({"source_module": "recent", "content": "the newest winner"})
    _cycle(None)

    seen = {}

    def _q(trigger, domain, **kw):
        seen["content"] = trigger.get("content")
        return {"text": "why?", "answer_source_hint": "none"}

    with patch.object(engine, "generate_question", _q):
        engine.reflect_once()

    assert seen["content"] == "the newest winner"


def test_only_idle_cycles_means_nothing_to_reflect_on():
    _cycle(None)
    _cycle(None)
    assert engine.reflect_once() is None
    assert [e["reason"] for e in _events("reflection_skipped")] == ["no_subject"]


def test_a_stale_subject_is_refused_before_any_thinking():
    """The subject comes out of the journal, so it IS a memory -- reasoning on
    top of one the live probe contradicts is the confabulation this whole
    freshness gate exists to stop."""
    _cycle({"source_module": "ops", "content": "the api on :8000 is still down"})

    def _must_not_run(*a, **kw):
        raise AssertionError("must not generate a question about a stale subject")

    with patch.object(engine, "is_stale_operational", lambda *_: True), \
         patch.object(engine, "generate_question", _must_not_run):
        assert engine.reflect_once() is None

    skipped = _events("reflection_skipped")
    assert skipped and skipped[-1]["reason"] == "stale_subject"


# ---------------------------------------------------------------------------
# the reflection itself
# ---------------------------------------------------------------------------

def test_a_completed_reflection_is_journaled():
    _cycle({"source_module": "repo", "content": "issue #12 reopened"})

    with patch.object(engine, "generate_question",
                      lambda *a, **kw: {"text": "does the floor still calibrate?",
                                        "answer_source_hint": "none"}), \
         patch.object(engine, "answer_question",
                      lambda *a, **kw: {"source": "none", "content": "no source",
                                        "success": False, "latency_ms": 3}):
        result = engine.reflect_once()

    assert result["question"] == "does the floor still calibrate?"
    logged = _events("reflection")
    assert len(logged) == 1 and logged[0]["answered"] is False


def test_kill_switch_restores_the_old_do_nothing_behaviour(monkeypatch):
    monkeypatch.setenv("ZUGAMIND_REFLECTION_ENABLED", "false")
    _cycle({"source_module": "repo", "content": "something happened"})

    def _must_not_run(*a, **kw):
        raise AssertionError("disabled reflection must make no model call")

    with patch.object(engine, "generate_question", _must_not_run):
        assert engine.reflect_once() is None
        assert engine.drift_integrity() is None
    assert _events("reflection") == []


def test_reflection_never_raises_into_the_cycle():
    _cycle({"source_module": "repo", "content": "x"})
    with patch.object(engine, "classify_domain", side_effect=RuntimeError("boom")), \
         patch.object(engine, "generate_question", lambda *a, **kw: None):
        assert engine.reflect_once() is None  # classify failure defaults, no raise


# ---------------------------------------------------------------------------
# the reflection is a candidate thought, and share_filter sits in front of it
# ---------------------------------------------------------------------------

def _reflect_with(answer, question="does the floor still calibrate?"):
    _cycle({"source_module": "repo", "content": "issue #12 reopened"})
    with patch.object(engine, "generate_question",
                      lambda *a, **kw: {"text": question,
                                        "answer_source_hint": "none"}), \
         patch.object(engine, "answer_question", lambda *a, **kw: answer):
        return engine.reflect_once()


def test_an_unanswered_reflection_never_becomes_a_shared_thought(monkeypatch):
    """No source resolved the question, so the pair is a guess. Its
    confidence lands under the share floor and the guard's reason -- which
    only share_filter produces -- must reach the journal, not vanish."""
    monkeypatch.setenv("ZUGAMIND_THOUGHTS_ENABLED", "true")

    result = _reflect_with({"source": "none", "content": "", "success": False,
                            "latency_ms": 1})

    assert result["confidence"] == 0.2
    assert _events("thought_shared") == []
    suppressed = _events("thought_suppressed")
    assert len(suppressed) == 1
    assert suppressed[0]["reason"] == "low_confidence:0.20"


def test_an_answered_reflection_reaches_the_journal_as_a_shared_question(monkeypatch):
    monkeypatch.setenv("ZUGAMIND_THOUGHTS_ENABLED", "true")

    _reflect_with({"source": "code_search", "content": "calibrator.py:40 ...",
                   "success": True, "latency_ms": 12})

    shared = _events("thought_shared")
    assert len(shared) == 1
    assert shared[0]["text"].startswith("does the floor still calibrate?")
    assert shared[0]["topic_class"] == "question"
    assert _events("thought_suppressed") == []


# ---------------------------------------------------------------------------
# grounding
# ---------------------------------------------------------------------------

def test_live_state_block_reaches_the_question_prompt():
    captured = {}

    def _fake_ollama(prompt, max_tokens=120, system="", **kw):
        captured["prompt"] = prompt
        return '{"text": "q?", "answer_source_hint": "none"}'

    generate_question({"content": "x"}, "OPERATIONAL",
                      grounding="VERIFIED LIVE STATE (probed now, 10:00:00):\n"
                                "  services UP: ledger(:9101)",
                      ollama_query_fn=_fake_ollama)
    assert "VERIFIED LIVE STATE" in captured["prompt"]
    assert captured["prompt"].index("VERIFIED LIVE STATE") < captured["prompt"].index("Trigger:")


def test_no_configured_services_adds_no_block():
    captured = {}

    def _fake_ollama(prompt, max_tokens=120, system="", **kw):
        captured["prompt"] = prompt
        return '{"text": "q?", "answer_source_hint": "none"}'

    generate_question({"content": "x"}, "SELF", grounding="",
                      ollama_query_fn=_fake_ollama)
    assert "VERIFIED LIVE STATE" not in captured["prompt"], \
        "an empty service map must add nothing -- no block beats a false one"


# ---------------------------------------------------------------------------
# drift integrity: the series that had no reader
# ---------------------------------------------------------------------------

def _drift(harness, values):
    prev = None
    for v in values:
        journal.append_event("floor_drifted", {
            "harness": harness, "basis": "raw", "from": prev, "to": v,
            "at_ceiling": False,
        })
        prev = v


def test_a_rising_floor_is_reported_as_drift():
    _drift("claude-code", [0.40 + i * 0.02 for i in range(14)])
    report = engine.drift_integrity()

    assert report is not None and report["harness"] == "claude-code"
    assert report["severity"] in ("CRITICAL", "DRIFTING")
    logged = _events("drift_integrity")
    assert logged and logged[-1]["harness"] == "claude-code"


def test_below_the_sample_floor_nothing_is_claimed():
    _drift("claude-code", [0.4, 0.45, 0.5])
    assert engine.drift_integrity() is None
    assert _events("drift_integrity") == []


def test_harnesses_are_judged_separately():
    """Two harnesses drifting opposite ways must not average into a calm line."""
    _drift("rising", [0.30 + i * 0.03 for i in range(14)])
    _drift("falling", [0.90 - i * 0.03 for i in range(14)])

    engine.drift_integrity()
    reported = {e["harness"] for e in _events("drift_integrity")}
    assert "rising" in reported, "a real upward trend must not be cancelled out"


def test_a_flat_floor_reports_nothing():
    _drift("steady", [0.5] * 14)
    engine.drift_integrity()
    assert _events("drift_integrity") == [], "a flat series is not a finding"


# ---------------------------------------------------------------------------
# the wiring itself
# ---------------------------------------------------------------------------

def test_runner_reflects_on_the_tenth_idle_cycle():
    from stream.runner import REFLECT_EVERY_N_IDLE, StreamRunner

    runner = StreamRunner.__new__(StreamRunner)
    runner._idle_cycles = REFLECT_EVERY_N_IDLE - 1

    calls = []
    with patch.object(StreamRunner, "_reflect", lambda self: calls.append(1)):
        state = runner._transition_state(None)

    assert state["state"] == "REFLECTING"
    assert calls == [1], "the REFLECTING transition must actually think"


def test_runner_does_not_reflect_on_an_ordinary_idle_cycle():
    from stream.runner import StreamRunner

    runner = StreamRunner.__new__(StreamRunner)
    runner._idle_cycles = 0

    calls = []
    with patch.object(StreamRunner, "_reflect", lambda self: calls.append(1)):
        state = runner._transition_state(None)

    assert state["state"] == "RESTING"
    assert calls == []
