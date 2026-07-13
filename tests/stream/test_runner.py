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
import act.floor_calibration as floor_calibration
import continuity.journal as journal
import foundation.config as config
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
    # Isolate quiet hours: load_quiet_hours() reads the REAL default config
    # (data/harness.json), which a live deployment on the dev machine may
    # own. Without this, any test running inside the operator's configured
    # quiet window fails with quiet_hours_deferred instead of reaching the
    # gate — observed 2026-07-12 at 23:0x, the same config-leak class that
    # contaminated EXP-002/003 (see run_exp001.py). Tests that exercise
    # quiet hours re-patch this explicitly.
    monkeypatch.setattr(command_actuator, "load_quiet_hours", lambda *a, **kw: None)


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


# --- PAUSE kill-switch --------------------------------------------------------

def test_pause_file_halts_the_whole_cycle(tmp_path, monkeypatch):
    _patch_engine_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [])
    pause_file = tmp_path / "PAUSE"
    monkeypatch.setattr(config, "PAUSE_FILE", pause_file)

    calls = {"n": 0}

    def _counting_scanner():
        calls["n"] += 1
        return [{"type": "local_service_down", "service": "x", "port": 1,
                 "detail": "x down"}]

    runner = StreamRunner(extra_scanners={"scan_counting": _counting_scanner},
                          dry_run=True, include_default_scanners=False)

    pause_file.touch()
    result = runner.run_once()

    assert result["paused"] is True
    assert result["harness_results"] == []
    assert calls["n"] == 0  # perception halted too — unlike quiet hours
    events = journal.read_events()
    assert len([e for e in events if e["kind"] == "paused"]) == 1
    assert not any(e["kind"] == "cycle" for e in events)

    # Paused again: no second "paused" event (journaled once per transition).
    runner.run_once()
    assert len([e for e in journal.read_events() if e["kind"] == "paused"]) == 1

    # rm PAUSE resumes on the next cycle, no restart, and journals "resumed".
    pause_file.unlink()
    result = runner.run_once()
    assert "paused" not in result
    assert calls["n"] == 1
    events = journal.read_events()
    assert any(e["kind"] == "resumed" for e in events)
    assert any(e["kind"] == "cycle" for e in events)


# --- habituation wiring -------------------------------------------------------

def test_default_scanner_triggers_are_habituated_across_cycles(tmp_path, monkeypatch):
    """A default world-scanner re-emitting the same trigger is damped on the
    second cycle; an injected extra_scanner emitting the same shape is not."""
    _patch_engine_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [])
    monkeypatch.setattr(config, "SEEN_TRIGGERS_FILE", tmp_path / "seen_triggers.json")

    def _repeat_story_scanner():
        return [{"type": "hn_story", "story_id": 7, "detail": "same story",
                 "novelty": 0.8, "relevance": 0.7, "urgency": 0.3}]

    # Stand in for the default world-scanners (offline) and disable discovery.
    monkeypatch.setattr(runner_mod, "_STATIC_SCANNERS", {"scan_toy_world": _repeat_story_scanner})
    monkeypatch.setattr(runner_mod, "discover_dynamic_scanners", lambda: {})

    runner = StreamRunner(dry_run=True)  # include_default_scanners=True
    assert runner.run_once()["trigger_count"] == 1
    assert runner.run_once()["trigger_count"] == 0  # damped: seen 1 cycle ago

    # Same trigger via extra_scanners: exempt (verify_harness relies on this).
    runner2 = StreamRunner(extra_scanners={"scan_injected": _repeat_story_scanner},
                           dry_run=True, include_default_scanners=False)
    assert runner2.run_once()["trigger_count"] == 1
    assert runner2.run_once()["trigger_count"] == 1  # not damped


# --- post-hoc integrity wiring ------------------------------------------------

def _winner(ttype="local_service_down"):
    return {"source_module": "infrastructure", "salience": 0.9,
            "content": "toy-api not responding",
            "context": {"triggers": [{"type": ttype, "detail": "toy-api down"}]}}


def test_value_prior_modulator_is_registered(tmp_path, monkeypatch):
    """The value-gate prior runs inside the workspace's modulator pass —
    late-bound, so patching the runner module's reference intercepts it."""
    _patch_engine_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [])

    seen = {"called": False}

    def _spy_prior(bids, db_path=None):
        seen["called"] = True
        return bids, None

    monkeypatch.setattr(runner_mod, "_apply_value_prior", _spy_prior)

    runner = StreamRunner(extra_scanners={"scan_toy_infra": _toy_infra_scanner},
                          dry_run=True, include_default_scanners=False)
    runner.run_once()

    assert seen["called"] is True


def test_work_claim_runs_on_real_harness_reply_and_journals(tmp_path, monkeypatch):
    _patch_engine_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(
        runner_mod, "check_work_claim",
        lambda text, **kw: {"backed": False,
                            "unbacked": ["I refactored the flux capacitor."],
                            "reason": "work_claim_no_matching_commit", "commits": 0},
    )
    scored = {}
    monkeypatch.setattr(runner_mod, "score_action",
                        lambda **kw: scored.update(kw))

    runner = StreamRunner(dry_run=True, include_default_scanners=False)
    runner._post_action_integrity(_winner(), [
        {"ok": True, "dry_run": False, "harness": "toy",
         "stdout": "I refactored the flux capacitor."},
    ])

    events = journal.read_events()
    wc = [e for e in events if e["kind"] == "work_claim"]
    assert len(wc) == 1
    assert wc[0]["backed"] is False
    assert wc[0]["harness"] == "toy"
    # The wake itself was scored with the winner's auction key.
    assert scored["source_module"] == "infrastructure"
    assert scored["trigger_type"] == "local_service_down"


def test_integrity_skips_dry_run_and_failed_invocations(tmp_path, monkeypatch):
    _patch_engine_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(
        runner_mod, "check_work_claim",
        lambda text, **kw: (_ for _ in ()).throw(AssertionError("must not be called")),
    )
    scored = {"called": False}
    monkeypatch.setattr(runner_mod, "score_action",
                        lambda **kw: scored.update(called=True))

    runner = StreamRunner(dry_run=True, include_default_scanners=False)
    runner._post_action_integrity(_winner(), [
        {"ok": True, "dry_run": True, "harness": "a", "stdout": "I fixed everything."},
        {"ok": False, "dry_run": False, "harness": "b", "stdout": "I shipped it."},
    ])

    assert scored["called"] is False  # no real invocation — nothing to score
    assert not any(e["kind"] == "work_claim" for e in journal.read_events())


def test_dispatch_records_ambient_sample_for_calibrate_mode_harness(tmp_path, monkeypatch):
    """Through-the-runner wiring check (issue #12) — the direct-dict
    _harness_wants tests in test_wake_filter.py can't catch a broken call
    site the way test_wake_filters_survive_the_config_loader catches a
    broken loader."""
    _patch_engine_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(floor_calibration, "STATE_FILE", tmp_path / "floor_calibration.json")
    harness_config = {
        "name": "calib-harness", "command": ["x", "{briefing_file}"],
        "timeout_sec": 10, "max_per_hour": 4, "enabled": True,
        "wake_min_salience": "calibrate",
    }
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [harness_config])

    runner = StreamRunner(extra_scanners={"scan_toy_infra": _toy_infra_scanner}, dry_run=True,
                          include_default_scanners=False)
    runner.run_once()

    state = json.loads(floor_calibration.STATE_FILE.read_text())
    assert len(state["calib-harness"]["samples"]) == 1
