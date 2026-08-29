"""Tests for the outbound-thought path (the share_filter socket, 2026-08-29).

share_filter.should_share() existed with no caller: there was no path a
candidate thought travelled down, so there was nothing for it to guard. This
is that path. Decision 1 (Buga, 2026-08-29): a shared thought goes to the
journal only -- a `thought_shared` event a human reads later -- and every
drop is a `thought_suppressed` event carrying the guard's reason.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from cognition import thoughts
from continuity import journal


def _events(kind):
    return [e for e in journal.read_events(limit=500) if e.get("kind") == kind]


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("ZUGAMIND_THOUGHTS_ENABLED", "true")


def _question(text="is the floor still calibrating?", confidence=0.9):
    return {"text": text, "confidence": confidence, "topic_class": "question"}


# ---------------------------------------------------------------------------
# the guard is in the path, and its verdict is recorded either way
# ---------------------------------------------------------------------------

def test_a_low_confidence_thought_is_suppressed_with_the_guards_reason(enabled):
    verdict = thoughts.consider_thought(_question(confidence=0.2))

    assert verdict["shared"] is False
    assert _events("thought_shared") == []
    suppressed = _events("thought_suppressed")
    assert len(suppressed) == 1
    assert suppressed[0]["reason"] == "low_confidence:0.20"


def test_a_share_worthy_thought_is_shared_when_enabled(enabled):
    verdict = thoughts.consider_thought(_question())

    assert verdict["shared"] is True
    shared = _events("thought_shared")
    assert len(shared) == 1
    assert shared[0]["text"] == "is the floor still calibrating?"
    assert _events("thought_suppressed") == []


# ---------------------------------------------------------------------------
# dark-ship: off by default, telemetry still emitted (value_gate pattern)
# ---------------------------------------------------------------------------

def test_dark_by_default_never_shares_but_still_records_the_verdict(monkeypatch):
    monkeypatch.delenv("ZUGAMIND_THOUGHTS_ENABLED", raising=False)

    verdict = thoughts.consider_thought(_question())

    assert verdict["shared"] is False
    assert _events("thought_shared") == []
    suppressed = _events("thought_suppressed")
    assert len(suppressed) == 1
    # The guard's own verdict survives, so "would have shared" is verifiable
    # from the journal while the flag is off.
    assert suppressed[0]["reason"] == "share"
    assert suppressed[0]["enabled"] is False


# ---------------------------------------------------------------------------
# fail-open: a crashing guard must not silence the agent (invariant 3)
# ---------------------------------------------------------------------------

def test_a_crashing_guard_fails_open_and_says_so(enabled):
    with patch.object(thoughts, "should_share", side_effect=RuntimeError("boom")):
        verdict = thoughts.consider_thought(_question())

    assert verdict["shared"] is True
    shared = _events("thought_shared")
    assert len(shared) == 1
    assert shared[0]["reason"].startswith("guard_error:")
