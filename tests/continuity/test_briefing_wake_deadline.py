"""`_wake_deadline_note` — the line telling a woken session when it gets killed.

Why this file exists: the note was added with no test of its own, and it was
appended between `if since_iso:` and that `if`'s `else:`, which silently
re-parented the `else` onto `if deadline_note:`. Both resulting defects are
invisible to the rest of the suite because the shared `jf` fixture stubs
`load_harness_configs` to `[]`, so the deadline branch never runs there:

  * no budget + a real prior wake  -> briefing claims BOTH "1h 32m since last
    wake" and "no prior wake recorded (first briefing)";
  * a budget + no prior wake       -> the "first briefing" line vanishes and
    the briefing reports no wake cursor at all.

The header is the first thing a woken session reads, so it has to be able to
state one thing about elapsed time and be right about it.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import act.command_actuator as command_actuator
import continuity.journal as journal
import foundation.state as state_mod

NOW = datetime(2026, 9, 8, 18, 38, 0, tzinfo=timezone.utc)


@pytest.fixture()
def jf(tmp_path, monkeypatch):
    path = tmp_path / "journal.jsonl"
    monkeypatch.setattr(journal, "JOURNAL_FILE", path)
    monkeypatch.setattr(journal, "_appends_since_check", 0)
    monkeypatch.setattr(journal, "_tail_checked_for", None)
    monkeypatch.setattr(state_mod, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(state_mod, "ENGINE_DIR", tmp_path)
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [])
    monkeypatch.delenv("ZUGAMIND_BRIEFING_MAX_CHARS", raising=False)
    return path


def _harnesses(monkeypatch, configs):
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: configs)


# --- the note itself ---------------------------------------------------------------------

def test_note_quotes_the_soonest_kill_across_dispatching_harnesses(jf, monkeypatch):
    _harnesses(monkeypatch, [{"name": "claude-code", "enabled": True, "timeout_sec": 900},
                             {"name": "openclaw", "enabled": True, "timeout_sec": 1800}])
    note = journal._wake_deadline_note(None, NOW)
    assert "18:53:00Z" in note and "15m from now" in note      # min(900, 1800), not the mean or max
    assert "DISCARDED" in note


def test_note_ignores_a_harness_that_is_not_dispatching(jf, monkeypatch):
    _harnesses(monkeypatch, [{"name": "claude-code", "enabled": True, "timeout_sec": 900},
                             {"name": "openclaw", "enabled": True, "timeout_sec": 60}])
    note = journal._wake_deadline_note(["claude-code"], NOW)
    assert "18:53:00Z" in note and "15m from now" in note      # the 60s harness is not being woken


def test_note_ignores_a_disabled_harness(jf, monkeypatch):
    _harnesses(monkeypatch, [{"name": "off", "enabled": False, "timeout_sec": 60},
                             {"name": "claude-code", "enabled": True, "timeout_sec": 900}])
    assert "15m from now" in journal._wake_deadline_note(None, NOW)


def test_note_is_absent_when_no_harness_declares_a_budget(jf, monkeypatch):
    _harnesses(monkeypatch, [{"name": "claude-code", "enabled": True, "timeout_sec": 0}])
    assert journal._wake_deadline_note(None, NOW) is None


def test_note_never_breaks_the_briefing_when_the_actuator_raises(jf, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("harness config unreadable")
    monkeypatch.setattr(command_actuator, "load_harness_configs", boom)
    assert journal._wake_deadline_note(None, NOW) is None


def test_note_reads_a_naive_now_as_utc(jf, monkeypatch):
    _harnesses(monkeypatch, [{"name": "claude-code", "enabled": True, "timeout_sec": 900}])
    assert "18:53:00Z" in journal._wake_deadline_note(None, NOW.replace(tzinfo=None))


# --- the header it lives in --------------------------------------------------------------

def test_a_real_prior_wake_is_not_also_called_a_first_briefing(jf, monkeypatch):
    """No budget to quote must not turn a 38m-old cursor into 'no prior wake'."""
    _harnesses(monkeypatch, [{"name": "claude-code", "enabled": True, "timeout_sec": 0}])
    text = journal.build_briefing("2026-09-08T18:00:00+00:00", winner=None, now=NOW)
    assert "**Time since last wake:** 38m" in text
    assert "first briefing" not in text


def test_the_first_briefing_line_survives_a_deadline_note(jf, monkeypatch):
    """A quotable budget must not swallow the no-prior-wake line."""
    _harnesses(monkeypatch, [{"name": "claude-code", "enabled": True, "timeout_sec": 900}])
    text = journal.build_briefing(None, winner=None, now=NOW)
    assert "first briefing" in text
    assert "15m from now" in text


def test_header_states_elapsed_exactly_once_with_both_present(jf, monkeypatch):
    _harnesses(monkeypatch, [{"name": "claude-code", "enabled": True, "timeout_sec": 900}])
    text = journal.build_briefing("2026-09-08T18:00:00+00:00", winner=None, now=NOW)
    assert sum(l.startswith("**Time since last wake:**") for l in text.splitlines()) == 1
    assert "38m" in text and "15m from now" in text and "first briefing" not in text
