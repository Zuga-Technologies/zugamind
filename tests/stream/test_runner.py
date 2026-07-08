"""Tests for stream/runner.py — the always-on cognition loop.

One --once cycle (via StreamRunner.run_once()) with injected toy scanners:
asserts a "cycle" journal event is written, the state file is updated, and
— with a dry_run harness config in place — a harness_invocation dry-run
event appears. Also covers the no-winner idle/REFLECTING state path and
the quiet-hours suppression + deferred-winner-resurfacing behavior.
"""
from __future__ import annotations

import json
from datetime import datetime

import act.command_actuator as command_actuator
import continuity.journal as journal
import foundation.state as state_mod
import stream.runner as runner_mod
from stream.runner import StreamRunner


def _toy_infra_scanner():
    return [{
        "type": "local_service_down",
        "service": "toy-api",
        "port": 9999,
        "detail": "toy-api not responding",
    }]


def _patch_engine_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    monkeypatch.setattr(state_mod, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(state_mod, "ENGINE_DIR", tmp_path)


def test_once_cycle_writes_cycle_event_and_state(tmp_path, monkeypatch):
    _patch_engine_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [])

    runner = StreamRunner(extra_scanners={"scan_toy_infra": _toy_infra_scanner}, dry_run=True, include_default_scanners=False)
    result = runner.run_once()

    assert result["trigger_count"] == 1
    assert result["winner"] is not None

    events = journal.read_events()
    cycle_events = [e for e in events if e["kind"] == "cycle"]
    assert len(cycle_events) == 1
    assert cycle_events[0]["trigger_count"] == 1
    assert cycle_events[0]["winner"] is not None
    assert "bids" in cycle_events[0]

    assert state_mod.STATE_FILE.exists()
    saved_state = json.loads(state_mod.STATE_FILE.read_text())
    assert saved_state["state"] in ("FOCUSED", "ALERT")


def test_once_cycle_dry_run_harness_invocation_appears(tmp_path, monkeypatch):
    _patch_engine_dir(tmp_path, monkeypatch)
    harness_config = {
        "name": "toy-harness",
        "command": ["toy-harness", "{briefing_file}"],
        "timeout_sec": 10,
        "max_per_hour": 4,
        "enabled": True,
    }
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [harness_config])

    runner = StreamRunner(extra_scanners={"scan_toy_infra": _toy_infra_scanner}, dry_run=True, include_default_scanners=False)
    result = runner.run_once()

    assert len(result["harness_results"]) == 1
    hr = result["harness_results"][0]
    assert hr["ok"] is True
    assert hr["dry_run"] is True

    events = journal.read_events()
    invocations = [e for e in events if e["kind"] == "harness_invocation"]
    assert len(invocations) == 1
    assert invocations[0]["dry_run"] is True


def test_disabled_harness_is_skipped(tmp_path, monkeypatch):
    _patch_engine_dir(tmp_path, monkeypatch)
    harness_config = {
        "name": "off-harness", "command": ["x", "{briefing_file}"],
        "timeout_sec": 10, "max_per_hour": 4, "enabled": False,
    }
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [harness_config])

    runner = StreamRunner(extra_scanners={"scan_toy_infra": _toy_infra_scanner}, dry_run=True, include_default_scanners=False)
    result = runner.run_once()
    assert result["harness_results"] == []


def test_no_modules_no_triggers_transitions_to_resting(tmp_path, monkeypatch):
    _patch_engine_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [])
    # An empty module set means Workspace.run_cycle() legitimately returns
    # None (no bids at all) — the shipped create_all_modules() always bids
    # via its intrinsic modules, so this isolates the RESTING branch.
    monkeypatch.setattr(runner_mod, "create_all_modules", lambda: [])

    runner = StreamRunner(dry_run=True, include_default_scanners=False)
    result = runner.run_once()

    assert result["winner"] is None
    assert result["state"] == "RESTING"
    assert result["harness_results"] == []


def test_tenth_consecutive_idle_cycle_transitions_to_reflecting(tmp_path, monkeypatch):
    _patch_engine_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [])
    monkeypatch.setattr(runner_mod, "create_all_modules", lambda: [])

    runner = StreamRunner(dry_run=True, include_default_scanners=False)
    results = runner.run_cycles(10)

    assert all(r["state"] == "RESTING" for r in results[:9])
    assert results[9]["state"] == "REFLECTING"


def test_gate_block_means_no_harness_invocation(tmp_path, monkeypatch):
    _patch_engine_dir(tmp_path, monkeypatch)
    harness_config = {
        "name": "blocked-harness", "command": ["x", "{briefing_file}"],
        "timeout_sec": 10, "max_per_hour": 4, "enabled": True,
    }
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [harness_config])
    monkeypatch.setattr(
        runner_mod, "escalate_for_action",
        lambda intent, dry_run=False: {"ok": False, "reason": "budget_exhausted"},
    )

    runner = StreamRunner(extra_scanners={"scan_toy_infra": _toy_infra_scanner}, dry_run=True, include_default_scanners=False)
    result = runner.run_once()

    assert result["harness_results"] == []
    events = journal.read_events()
    assert any(e["kind"] == "harness_skip" for e in events)


# --- quiet hours -------------------------------------------------------------

def test_is_quiet_hours_simple_same_day_window():
    quiet = {"start": "09:00", "end": "17:00"}
    assert runner_mod.is_quiet_hours(quiet, datetime(2026, 1, 1, 12, 0)) is True
    assert runner_mod.is_quiet_hours(quiet, datetime(2026, 1, 1, 8, 0)) is False
    assert runner_mod.is_quiet_hours(quiet, datetime(2026, 1, 1, 17, 0)) is False


def test_is_quiet_hours_wraps_past_midnight():
    quiet = {"start": "23:00", "end": "07:00"}
    assert runner_mod.is_quiet_hours(quiet, datetime(2026, 1, 1, 23, 30)) is True
    assert runner_mod.is_quiet_hours(quiet, datetime(2026, 1, 2, 3, 0)) is True
    assert runner_mod.is_quiet_hours(quiet, datetime(2026, 1, 2, 6, 59)) is True
    assert runner_mod.is_quiet_hours(quiet, datetime(2026, 1, 2, 7, 0)) is False
    assert runner_mod.is_quiet_hours(quiet, datetime(2026, 1, 1, 12, 0)) is False


def test_is_quiet_hours_no_config_never_quiet():
    assert runner_mod.is_quiet_hours(None, datetime(2026, 1, 1, 23, 30)) is False


def test_quiet_hours_suppresses_harness_invocation_and_journals_deferred(tmp_path, monkeypatch):
    _patch_engine_dir(tmp_path, monkeypatch)
    harness_config = {
        "name": "quiet-harness", "command": ["x", "{briefing_file}"],
        "timeout_sec": 10, "max_per_hour": 4, "max_per_day": 20, "enabled": True,
    }
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [harness_config])
    monkeypatch.setattr(command_actuator, "load_quiet_hours",
                        lambda *a, **kw: {"start": "23:00", "end": "07:00"})

    runner = StreamRunner(extra_scanners={"scan_toy_infra": _toy_infra_scanner}, dry_run=True,
                          include_default_scanners=False)
    result = runner.run_once(now=datetime(2026, 1, 1, 23, 30))

    assert result["harness_results"] == []
    events = journal.read_events()
    deferred = [e for e in events if e["kind"] == "quiet_hours_deferred"]
    assert len(deferred) == 1
    assert deferred[0]["harness"] == "quiet-harness"
    assert deferred[0]["winner"] is not None
    # No harness_invocation and no harness_skip — this is a deferral, not a gate refusal.
    assert not any(e["kind"] == "harness_invocation" for e in events)
    assert not any(e["kind"] == "harness_skip" for e in events)


def test_quiet_hours_does_not_suppress_perception_or_state(tmp_path, monkeypatch):
    _patch_engine_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [])
    monkeypatch.setattr(command_actuator, "load_quiet_hours",
                        lambda *a, **kw: {"start": "23:00", "end": "07:00"})

    runner = StreamRunner(extra_scanners={"scan_toy_infra": _toy_infra_scanner}, dry_run=True,
                          include_default_scanners=False)
    result = runner.run_once(now=datetime(2026, 1, 1, 23, 30))

    assert result["trigger_count"] == 1  # scanner still ran
    events = journal.read_events()
    assert any(e["kind"] == "cycle" for e in events)  # journaling never stops
    assert state_mod.STATE_FILE.exists()  # state machine still updates


def test_deferred_winner_surfaces_in_next_real_briefing(tmp_path, monkeypatch):
    _patch_engine_dir(tmp_path, monkeypatch)
    harness_config = {
        "name": "quiet-harness-2", "command": ["x", "{briefing_file}"],
        "timeout_sec": 10, "max_per_hour": 4, "max_per_day": 20, "enabled": True,
    }
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [harness_config])

    quiet_state = {"active": True}
    monkeypatch.setattr(
        command_actuator, "load_quiet_hours",
        lambda *a, **kw: ({"start": "23:00", "end": "07:00"} if quiet_state["active"] else None),
    )

    runner = StreamRunner(extra_scanners={"scan_toy_infra": _toy_infra_scanner}, dry_run=True,
                          include_default_scanners=False)

    # Cycle during quiet hours: deferred, no real invocation.
    r1 = runner.run_once(now=datetime(2026, 1, 1, 23, 30))
    assert r1["harness_results"] == []

    # Capture the briefing text actually handed to invoke_harness (the temp
    # file it's written to is cleaned up before invoke_harness returns, so
    # this is the only place to observe its contents post-hoc).
    captured = {}
    real_invoke = command_actuator.invoke_harness

    def _capturing_invoke(config, briefing, dry_run=False):
        captured["briefing"] = briefing
        return real_invoke(config, briefing, dry_run=dry_run)

    monkeypatch.setattr(command_actuator, "invoke_harness", _capturing_invoke)

    # Quiet hours end; next cycle should actually wake the harness, and its
    # briefing should mention the deferred cycle from before.
    quiet_state["active"] = False
    r2 = runner.run_once(now=datetime(2026, 1, 2, 8, 0))
    assert len(r2["harness_results"]) == 1
    assert r2["harness_results"][0]["dry_run"] is True

    assert "Deferred during quiet hours" in captured["briefing"]
    assert "quiet-harness-2" in captured["briefing"]
