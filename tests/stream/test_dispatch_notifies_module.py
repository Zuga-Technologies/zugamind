"""A DISPATCHED session, not a workspace win, is what the winning module hears about.

Live shape (BugaPC, 2026-08-29..09-11, 2,048 journaled cycles): priority_goals
won the workspace 1,057 times and bought zero sessions, and 94% of those wins
bid 0.23-0.26 with the goal under an hour stale — its clock reset on every
win. Winning the workspace is free and happens every cycle; a harness run is
the event that costs money and can advance something. The runner now tells
the winning module about THAT event via on_dispatched(), only for results
that are ok and not dry-run, fail-open.

Run: python -m pytest tests/stream/test_dispatch_notifies_module.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta

import act.command_actuator as command_actuator
import act.floor_calibration as floor_calibration
import continuity.journal as journal
import foundation.state as state_mod
import stream.runner as runner_mod
from cognition.workspace.workspace import WorkspaceModule
from cognition.workspace.workspace_modules import PriorityGoalsModule
from stream.runner import StreamRunner

T0 = datetime(2026, 9, 11, 0, 0, 0)

HARNESS = {
    "name": "cc", "command": ["x", "{briefing_file}"],
    "timeout_sec": 10, "max_per_hour": 4, "enabled": True,
    "wake_min_salience": 0.6,
}


def _runner(tmp_path, monkeypatch, result):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    monkeypatch.setattr(state_mod, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(state_mod, "ENGINE_DIR", tmp_path)
    monkeypatch.setattr(floor_calibration, "STATE_FILE", tmp_path / "floor_calibration.json")
    monkeypatch.setattr(PriorityGoalsModule, "STATE_FILE", tmp_path / "priority_goals_state.json")
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [HARNESS])
    monkeypatch.setattr(command_actuator, "load_quiet_hours", lambda *a, **kw: None)
    monkeypatch.setattr(runner_mod, "escalate_for_action", lambda intent, dry_run=False: {"ok": True})
    monkeypatch.setattr(command_actuator, "invoke_harness",
                        lambda hc, briefing, dry_run=False: dict(result, harness=hc["name"]))
    runner = StreamRunner(dry_run=False, include_default_scanners=False)
    monkeypatch.setattr(runner.planner, "propose_plan", lambda content, budget: None)
    return runner


def _goals_module(runner):
    m = next(m for m in runner.modules if m.name == "priority_goals")
    m._now_fn = lambda: T0
    m.set_goals([{"key": "goal:47", "label": "Licensing lane", "actionable": True}])
    m._goal_last_touched["goal:47"] = T0 - timedelta(hours=48)  # two days stale: bids the 0.75 cap, over the 0.6 floor
    return m


def _winner(m):
    bid = m.generate_bid({})
    ctx = dict(bid.context, raw_salience=bid.salience)
    return {"source_module": "priority_goals", "salience": bid.salience,
            "content": bid.content, "context": ctx}


def _journal(tmp_path):
    """Why a dispatch returned nothing lives in the journal, not the result."""
    j = tmp_path / "journal.jsonl"
    return j.read_text(encoding="utf-8")[-1500:] if j.exists() else "<no journal written>"


def test_a_real_harness_run_resets_the_goal_clock(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch, {"ok": True, "dry_run": False, "stdout": ""})
    m = _goals_module(runner)
    results = runner._dispatch_to_harnesses(None, _winner(m), {})
    assert [r["ok"] for r in results] == [True], _journal(tmp_path)
    assert m._goal_last_touched["goal:47"] == T0


def test_a_dry_run_does_not_reset_the_goal_clock(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch, {"ok": True, "dry_run": True})
    m = _goals_module(runner)
    results = runner._dispatch_to_harnesses(None, _winner(m), {})
    assert [r["ok"] for r in results] == [True], _journal(tmp_path)
    assert m._goal_last_touched["goal:47"] == T0 - timedelta(hours=48)


def test_a_failed_harness_does_not_reset_the_goal_clock(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch, {"ok": False, "dry_run": False, "error": "rc=1"})
    m = _goals_module(runner)
    runner._dispatch_to_harnesses(None, _winner(m), {})
    assert m._goal_last_touched["goal:47"] == T0 - timedelta(hours=48)


class _Hooked(WorkspaceModule):
    name = "hooked"

    def __init__(self):
        super().__init__()
        self.seen = []

    def generate_bid(self, context):
        return None

    def on_dispatched(self, winner):
        self.seen.append(winner)


class _Plain(WorkspaceModule):
    name = "plain"

    def generate_bid(self, context):
        return None


class _Raises(WorkspaceModule):
    name = "raises"

    def generate_bid(self, context):
        return None

    def on_dispatched(self, winner):
        raise RuntimeError("module bug")


def test_notify_reaches_only_the_winning_module_and_only_for_real_ok_runs(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch, {"ok": True})
    hooked, other = _Hooked(), _Hooked()
    other.name = "other"
    runner.modules = [other, hooked, _Plain()]
    winner = {"source_module": "hooked", "context": {"target": "t"}}
    assert runner._notify_dispatched(winner, [{"ok": True, "dry_run": False}]) is True
    assert hooked.seen == [winner] and other.seen == []
    assert runner._notify_dispatched(winner, [{"ok": True, "dry_run": True}]) is False
    assert runner._notify_dispatched(winner, [{"ok": False}]) is False
    assert runner._notify_dispatched(winner, []) is False
    assert hooked.seen == [winner]
    # a module without a hook of its own falls through to the base no-op,
    # which is still "a hook was called" (and harmless)
    assert runner._notify_dispatched({"source_module": "plain"}, [{"ok": True}]) is True
    # an unknown winner is nobody's business
    assert runner._notify_dispatched({"source_module": "nobody"}, [{"ok": True}]) is False


def test_a_raising_hook_is_swallowed(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch, {"ok": True})
    runner.modules = [_Raises()]
    assert runner._notify_dispatched({"source_module": "raises"}, [{"ok": True}]) is False


def test_base_module_hook_is_a_no_op():
    WorkspaceModule().on_dispatched({"source_module": "base"})
