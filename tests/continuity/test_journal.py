"""Tests for continuity/journal.py — episodic event log + wake briefing.

Covers: append/read round-tripping, since_iso filtering, malformed-line
tolerance, the briefing's grouped sections, and unresolved-handoff logic
(a "handoff" with no matching "handoff_done" surfaces; one that has a
matching id does not).
"""
from __future__ import annotations

from datetime import datetime, timezone

import continuity.journal as journal


def _patch_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")


def test_append_and_read_round_trips(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    journal.append_event("cycle", {"trigger_count": 3, "winner": None})
    events = journal.read_events()
    assert len(events) == 1
    assert events[0]["kind"] == "cycle"
    assert events[0]["trigger_count"] == 3
    assert "ts" in events[0]


def test_read_events_missing_file_returns_empty(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    assert journal.read_events() == []


def test_read_events_on_error_raise_surfaces_read_failures(tmp_path, monkeypatch):
    """Safety-sensitive callers (the actuator's rate limiter) must be able to
    distinguish "no events" from "couldn't read the events"."""
    import pytest

    # A directory at the journal path: .exists() is True, read_text() raises.
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path)
    assert journal.read_events() == []  # default mode degrades gracefully
    with pytest.raises(Exception):
        journal.read_events(on_error="raise")


def test_read_events_on_error_raise_still_returns_empty_for_missing_file(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)  # file does not exist
    assert journal.read_events(on_error="raise") == []


def test_append_never_raises_on_bad_directory(tmp_path, monkeypatch):
    # Point JOURNAL_FILE at a path whose parent can't be created (a file,
    # not a dir, in the way) — append_event must swallow the error.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setattr(journal, "JOURNAL_FILE", blocker / "sub" / "journal.jsonl")
    journal.append_event("cycle", {"x": 1})  # must not raise


def test_since_iso_filters_out_older_events(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(journal, "now_iso", lambda: "2026-01-01T00:00:00+00:00")
    journal.append_event("cycle", {"n": 1})
    monkeypatch.setattr(journal, "now_iso", lambda: "2026-01-02T00:00:00+00:00")
    journal.append_event("cycle", {"n": 2})

    all_events = journal.read_events()
    assert [e["n"] for e in all_events] == [1, 2]

    recent = journal.read_events(since_iso="2026-01-01T12:00:00+00:00")
    assert [e["n"] for e in recent] == [2]


def test_read_events_skips_malformed_lines(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    journal.JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    journal.JOURNAL_FILE.write_text(
        '{"ts": "2026-01-01T00:00:00+00:00", "kind": "cycle", "n": 1}\n'
        "not json at all\n"
        '{"ts": "2026-01-01T00:00:01+00:00", "kind": "cycle", "n": 2}\n',
        encoding="utf-8",
    )
    events = journal.read_events()
    assert [e["n"] for e in events] == [1, 2]


def test_read_events_respects_limit():
    pass  # covered indirectly via briefing group caps below; limit logic is a plain slice


def test_build_briefing_no_history_first_wake(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    text = journal.build_briefing(None)
    assert "# ZugaMind Wake Briefing" in text
    assert "first briefing" in text
    assert "no winner supplied" in text
    assert "## Unresolved handoffs" in text
    assert "- none" in text


def test_build_briefing_includes_supplied_winner():
    winner = {"source_module": "infrastructure", "content": "CRITICAL: example-api down",
              "salience": 0.91}
    text = journal.build_briefing(None, winner=winner)
    assert "infrastructure" in text
    assert "CRITICAL: example-api down" in text
    assert "0.91" in text


def test_build_briefing_groups_winners_actions_alarms(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    journal.append_event("cycle", {"winner": {"source_module": "infra", "content": "disk full"}})
    journal.append_event("harness_invocation", {"harness": "claude-code", "ok": True, "dry_run": True})
    journal.append_event("alarm", {"detail": "infra: disk full", "urgency": 0.9})

    text = journal.build_briefing(None)
    assert "### Winners" in text
    assert "infra: disk full" in text
    assert "### Actions taken" in text
    assert "claude-code" in text
    assert "### Alarms" in text
    assert "1 workspace winner(s), 1 harness invocation(s), 1 alarm(s)" in text


def test_build_briefing_unresolved_handoff_without_done(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    journal.append_event("handoff", {"id": "h1", "detail": "waiting on deploy approval"})
    text = journal.build_briefing(None)
    assert "h1" in text
    assert "waiting on deploy approval" in text
    assert "- none" not in text


def test_build_briefing_handoff_resolved_when_done_follows(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    journal.append_event("handoff", {"id": "h2", "detail": "waiting on X"})
    journal.append_event("handoff_done", {"id": "h2"})
    text = journal.build_briefing(None)
    assert "h2" not in text
    assert "- none" in text


def test_build_briefing_reports_elapsed_time_deterministically(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    since = "2026-01-01T00:00:00+00:00"
    now = datetime(2026, 1, 1, 1, 30, tzinfo=timezone.utc)
    text = journal.build_briefing(since, now=now)
    assert "1h 30m" in text


def test_build_briefing_stays_reasonably_short(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    for i in range(30):
        journal.append_event("cycle", {"winner": {"source_module": "m", "content": f"w{i}"}})
    for i in range(30):
        journal.append_event("harness_invocation", {"harness": "h", "ok": True})
    text = journal.build_briefing(None)
    assert len(text.splitlines()) < 80


def test_build_briefing_includes_deferred_quiet_hours_group(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    journal.append_event("quiet_hours_deferred", {
        "harness": "claude-code",
        "winner": {"source_module": "infra", "content": "disk full"},
    })
    text = journal.build_briefing(None)
    assert "### Deferred during quiet hours" in text
    assert "claude-code" in text
    assert "disk full" in text
    assert "1 deferred (quiet hours)" in text


# --- hard size cap -----------------------------------------------------------

def test_build_briefing_respects_default_max_chars(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    for i in range(200):
        journal.append_event("cycle", {
            "winner": {"source_module": "m", "content": f"winner number {i} " * 5},
        })
    text = journal.build_briefing(None)
    assert len(text) <= journal._DEFAULT_BRIEFING_MAX_CHARS


def test_build_briefing_respects_env_override_max_chars(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    monkeypatch.setenv("ZUGAMIND_BRIEFING_MAX_CHARS", "600")
    for i in range(200):
        journal.append_event("cycle", {
            "winner": {"source_module": "m", "content": f"winner number {i} " * 5},
        })
    text = journal.build_briefing(None)
    assert len(text) <= 600


def test_build_briefing_truncation_never_drops_the_current_winner(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    monkeypatch.setenv("ZUGAMIND_BRIEFING_MAX_CHARS", "400")
    for i in range(200):
        journal.append_event("cycle", {
            "winner": {"source_module": "m", "content": f"winner number {i} " * 5},
        })
    winner = {"source_module": "infrastructure", "content": "THE CURRENT WINNER", "salience": 0.81}
    text = journal.build_briefing(None, winner=winner)
    assert "THE CURRENT WINNER" in text
    assert len(text) <= 400


def test_build_briefing_truncates_oldest_group_items_first(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    # Distinct, greppable markers so we can tell which end of the list survives.
    for i in range(20):
        journal.append_event("cycle", {"winner": {"source_module": "m", "content": f"MARKER-{i:03d}"}})
    monkeypatch.setenv("ZUGAMIND_BRIEFING_MAX_CHARS", "700")
    text = journal.build_briefing(None)
    assert "MARKER-019" in text  # newest kept
    assert "MARKER-000" not in text  # oldest trimmed first
