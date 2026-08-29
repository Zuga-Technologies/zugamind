"""The attention self-model survives a restart (2026-08-28).

AttentionSchema.to_dict/restore_from_dict existed with no caller: every
process restart reset streaks, win counts and blind spots. The snapshot now
rides inside state.json under "attention".
"""
from __future__ import annotations

import json

import act.command_actuator as command_actuator
import continuity.journal as journal
import foundation.state as state_mod
from stream.runner import StreamRunner


def _toy_scanner():
    return [{"type": "local_service_down", "service": "toy", "port": 1, "detail": "toy down"}]


def _patch(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    monkeypatch.setattr(state_mod, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(state_mod, "ENGINE_DIR", tmp_path)
    monkeypatch.setattr(command_actuator, "load_quiet_hours", lambda *a, **kw: None)
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [])


def _runner():
    return StreamRunner(extra_scanners={"scan_toy": _toy_scanner}, dry_run=True, include_default_scanners=False)


def test_snapshot_is_saved_each_cycle_and_restored_on_restart(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    first = _runner()
    for _ in range(4):
        first.run_once()
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["attention"]
    assert saved["total_cycles"] == 4
    assert sum(saved["module_win_counts"].values()) == 4  # streak dampening shares wins around

    second = _runner()  # a "restart"
    schema = second.workspace.attention_schema
    assert schema._total_cycles == 4
    assert schema.module_win_counts == saved["module_win_counts"]
    assert schema.recent_foci == saved["recent_foci"]
    assert schema.current_focus_module == saved["current_focus_module"]
    # only modules that have BID are in the win table (seeded on first bid)
    assert set(schema.module_win_counts) == set(saved["module_win_counts"])
    second.run_once()
    assert second.workspace.attention_schema._total_cycles == 5


def test_a_poisoned_snapshot_does_not_block_startup(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    (tmp_path / "state.json").write_text(json.dumps({
        "state": "RESTING", "since": None, "last_cycle": None, "cycles_today": 0,
        "last_transition": None, "focus_topic": None,
        "attention": {"recent_foci": "nope", "module_win_counts": [1], "total_cycles": "9"},
    }), encoding="utf-8")
    r = _runner()
    assert r.workspace.attention_schema._total_cycles == 0
    assert r.run_once()["winner"] is not None


def test_fresh_state_has_no_snapshot_and_starts_clean(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    r = _runner()
    assert r.workspace.attention_schema._total_cycles == 0
    r.run_once()
    assert "attention" in json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
