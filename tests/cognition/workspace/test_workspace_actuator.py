"""Tests for cognition/workspace/workspace_actuator.py — WorkspaceActuator.

This module is reference-only (issue #5): exported by workspace_actuator.py
itself but no longer re-exported from cognition.workspace's __all__, and not
instantiated by StreamRunner. Wiring it in would change bugapc-claude-
observer's live wake selection dynamics mid-EXP-005 observation window — the
same contamination class flagged for #4 and #12. These tests cover the class
directly, with zero prior coverage before this change.
"""
from __future__ import annotations

import json

import cognition.workspace.workspace_actuator as workspace_actuator
from cognition.workspace.workspace_actuator import WorkspaceActuator


class FakeAttentionSchema:
    """Minimal duck-typed stand-in — the actuator only reads these 3 attrs
    and calls set_adjustment()."""

    def __init__(self, win_counts, recent_foci, total_cycles):
        self.module_win_counts = win_counts
        self.recent_foci = recent_foci
        self._total_cycles = total_cycles
        self.adjustments: dict[str, float] = {}

    def set_adjustment(self, module: str, adj: float) -> None:
        self.adjustments[module] = adj


def _patch_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_actuator, "CPPS_FILE", tmp_path / "workspace_cpps.jsonl")
    monkeypatch.setattr(workspace_actuator, "ACTUATOR_STATE_FILE", tmp_path / "actuator_state.json")
    monkeypatch.setattr(workspace_actuator, "ENGINE_DIR", tmp_path)


def _run_to_check(actuator: WorkspaceActuator, stats, schema, cycle_start=1):
    """Drive on_cycle_complete until the Nth call triggers a real check."""
    result = {}
    for i in range(workspace_actuator.ACTUATOR_INTERVAL):
        result = actuator.on_cycle_complete(stats, schema, cycle_start + i)
    return result


# --- warmup / interval gating -------------------------------------------------

def test_returns_empty_before_interval_elapsed(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    actuator = WorkspaceActuator()
    schema = FakeAttentionSchema({}, [], total_cycles=20)
    result = actuator.on_cycle_complete({}, schema, 1)
    assert result == {}


def test_warmup_status_below_10_total_cycles(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    actuator = WorkspaceActuator()
    schema = FakeAttentionSchema({}, [], total_cycles=5)
    result = _run_to_check(actuator, {}, schema)
    assert result["status"] == "warmup"
    assert result["adjustments"] == {}


# --- starvation boost ----------------------------------------------------

def test_boosts_never_won_registered_module(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    actuator = WorkspaceActuator()
    schema = FakeAttentionSchema(
        win_counts={"repo_issues": 50},
        recent_foci=[{"module": "repo_issues"}] * 3,
        total_cycles=50,
    )
    stats = {"registered_modules": ["repo_issues", "starved_module"]}
    result = _run_to_check(actuator, stats, schema)
    assert result["adjustments"]["starved_module"] == 0.08
    assert schema.adjustments["starved_module"] == 0.08


def test_does_not_boost_metacognition(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    actuator = WorkspaceActuator()
    schema = FakeAttentionSchema({}, [], total_cycles=50)
    stats = {"registered_modules": ["metacognition"]}
    result = _run_to_check(actuator, stats, schema)
    assert "metacognition" not in result["adjustments"]


# --- domination penalty ----------------------------------------------------

def test_penalizes_dominant_module(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    actuator = WorkspaceActuator()
    recent = [{"module": "loud_module"}] * 8 + [{"module": "other"}] * 2
    schema = FakeAttentionSchema(
        win_counts={"loud_module": 40, "other": 10},
        recent_foci=recent,
        total_cycles=50,
    )
    result = _run_to_check(actuator, {"registered_modules": ["loud_module", "other"]}, schema)
    assert result["adjustments"]["loud_module"] < 0
    assert schema.adjustments["loud_module"] == max(
        workspace_actuator.MAX_PENALTY, result["adjustments"]["loud_module"]
    )


def test_no_penalty_under_5_recent_foci(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    actuator = WorkspaceActuator()
    schema = FakeAttentionSchema(
        win_counts={"x": 20},
        recent_foci=[{"module": "x"}] * 4,
        total_cycles=20,
    )
    result = _run_to_check(actuator, {"registered_modules": ["x"]}, schema)
    assert "x" not in result["adjustments"] or result["adjustments"]["x"] >= 0


# --- optional risk check ----------------------------------------------------

def test_risk_check_suppresses_action_modules_above_threshold(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    actuator = WorkspaceActuator(design_space_check=lambda: {"p_failure": 0.8})
    schema = FakeAttentionSchema({}, [], total_cycles=50)
    result = _run_to_check(actuator, {"registered_modules": []}, schema)
    assert result["adjustments"]["daemon"] < 0
    assert result["adjustments"]["code_changes"] < 0
    assert result["design_space"]["p_failure"] == 0.8


def test_risk_check_ignored_below_threshold(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    actuator = WorkspaceActuator(design_space_check=lambda: {"p_failure": 0.1})
    schema = FakeAttentionSchema({}, [], total_cycles=50)
    result = _run_to_check(actuator, {"registered_modules": []}, schema)
    assert "daemon" not in result["adjustments"]


def test_risk_check_exception_is_swallowed(tmp_path, monkeypatch):
    def raising():
        raise RuntimeError("model unavailable")

    _patch_dirs(tmp_path, monkeypatch)
    actuator = WorkspaceActuator(design_space_check=raising)
    schema = FakeAttentionSchema({}, [], total_cycles=50)
    result = _run_to_check(actuator, {"registered_modules": []}, schema)
    assert result["design_space"] is None


# --- CPP logging + state persistence -----------------------------------------

def test_cpps_logged_on_check(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    actuator = WorkspaceActuator()
    schema = FakeAttentionSchema({}, [], total_cycles=50)
    _run_to_check(actuator, {"cycle_count": 3, "registered_modules": []}, schema, cycle_start=100)

    lines = workspace_actuator.CPPS_FILE.read_text().splitlines()
    assert len(lines) == 1
    cpp = json.loads(lines[0])
    assert cpp["cycle"] == 100 + workspace_actuator.ACTUATOR_INTERVAL - 1
    assert cpp["workspace_cycle_count"] == 3


def test_state_persists_across_instances(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    schema = FakeAttentionSchema({}, [], total_cycles=50)
    first = WorkspaceActuator()
    _run_to_check(first, {"registered_modules": []}, schema)
    assert first._total_checks == 1

    second = WorkspaceActuator()
    assert second._total_checks == 1
    assert second._last_check_cycle == first._last_check_cycle


# --- reference-only, not part of the public surface --------------------------

def test_not_exported_from_workspace_package():
    import cognition.workspace as workspace_pkg
    assert "WorkspaceActuator" not in workspace_pkg.__all__
    assert not hasattr(workspace_pkg, "WorkspaceActuator")
