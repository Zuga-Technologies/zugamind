"""Tests for cognition/workspace/workspace_planner.py — WorkspacePlanner.

Covers: every plan-template route selected by _select_plan (single service
restart, systemic restart, production investigation, code analysis, task
retry, priority-goal advance, and the _plan_simple fallback), the
pending-task queue-depth gate (TASKS_FILE missing -> 0 pending; present ->
counted, filtered by status+source), and the low-budget one-step clamp
(budget remaining < $0.50 truncates a multi-step plan to 1 step).
"""
from __future__ import annotations

import json

import cognition.workspace.workspace_planner as workspace_planner
from cognition.workspace.workspace import SalienceBid, ThoughtType, WorkspaceContent
from cognition.workspace.workspace_planner import WorkspacePlanner


def _content(module: str, context: dict, salience: float = 0.9,
             text: str = "winner text") -> WorkspaceContent:
    bid = SalienceBid(module, text, salience, ThoughtType.INFRASTRUCTURE, context=context)
    return WorkspaceContent(bid=bid)


def _patch_tasks_file(tmp_path, monkeypatch):
    tasks_file = tmp_path / "tasks.json"
    monkeypatch.setattr(workspace_planner, "TASKS_FILE", tasks_file)
    return tasks_file


BUDGET_OK = {"remaining": 10.0}


# --- plan template routes ----------------------------------------------------

def test_single_service_restart_plan(tmp_path, monkeypatch):
    _patch_tasks_file(tmp_path, monkeypatch)
    content = _content("infrastructure", {
        "n_critical": 1,
        "triggers": [{"type": "local_service_down", "service": "toy-api", "port": 9999}],
    })
    plan = WorkspacePlanner().propose_plan(content, BUDGET_OK)

    assert len(plan) == 1
    assert plan[0]["action"] == "restart_service"
    assert "toy-api" in plan[0]["description"]
    assert "9999" in plan[0]["description"]
    assert plan[0]["step_index"] == 0
    assert plan[0]["total_steps"] == 1
    assert plan[0]["workspace_module"] == "infrastructure"
    assert plan[0]["workspace_salience"] == 0.9
    assert "planned_at" in plan[0]


def test_systemic_restart_plan_for_three_plus_critical(tmp_path, monkeypatch):
    _patch_tasks_file(tmp_path, monkeypatch)
    content = _content("infrastructure", {
        "n_critical": 3,
        "triggers": [{"type": "local_service_down", "service": "a"}],
    })
    plan = WorkspacePlanner().propose_plan(content, BUDGET_OK)

    assert len(plan) == 3
    assert [s["action"] for s in plan] == ["analyze", "restart_service", "analyze"]
    assert plan[1]["depends_on"] == 0
    assert plan[2]["depends_on"] == 1
    assert plan[0]["context"]["phase"] == "diagnose"
    assert plan[2]["context"]["phase"] == "verify"


def test_production_down_takes_priority_over_critical_count(tmp_path, monkeypatch):
    """A production_down trigger routes to prod-investigation even when
    n_critical alone would have selected the systemic-restart template."""
    _patch_tasks_file(tmp_path, monkeypatch)
    content = _content("infrastructure", {
        "n_critical": 5,
        "triggers": [{"type": "production_down", "endpoint": "/api/health"}],
    })
    plan = WorkspacePlanner().propose_plan(content, BUDGET_OK)

    assert len(plan) == 3
    assert [s["action"] for s in plan] == ["analyze", "code", "analyze"]
    assert "/api/health" in plan[0]["description"]
    assert plan[1]["depends_on"] == 0
    assert plan[2]["depends_on"] == 1


def test_code_changes_plan(tmp_path, monkeypatch):
    _patch_tasks_file(tmp_path, monkeypatch)
    content = _content("code_changes", {
        "triggers": [{"type": "code_change", "detail": "diff"}],
        "projects": ["Zugabot"],
    })
    plan = WorkspacePlanner().propose_plan(content, BUDGET_OK)

    assert len(plan) == 2
    assert [s["action"] for s in plan] == ["analyze", "code"]
    assert "Zugabot" in plan[0]["description"]
    assert plan[1]["depends_on"] == 0


def test_task_retry_plan_for_daemon_failure(tmp_path, monkeypatch):
    _patch_tasks_file(tmp_path, monkeypatch)
    content = _content("daemon", {
        "n_failures": 1,
        "triggers": [{"type": "daemon_task_failed", "detail": "worker crashed"}],
    })
    plan = WorkspacePlanner().propose_plan(content, BUDGET_OK)

    assert len(plan) == 2
    assert [s["action"] for s in plan] == ["analyze", "code"]
    assert "worker crashed" in plan[0]["description"]


def test_daemon_without_failures_falls_back_to_simple(tmp_path, monkeypatch):
    _patch_tasks_file(tmp_path, monkeypatch)
    content = _content("daemon", {"n_failures": 0, "triggers": []}, text="idle daemon note")
    plan = WorkspacePlanner().propose_plan(content, BUDGET_OK)

    assert len(plan) == 1
    assert plan[0]["action"] == "analyze"
    assert plan[0]["description"] == "idle daemon note"


def test_priority_goal_advance_plan(tmp_path, monkeypatch):
    _patch_tasks_file(tmp_path, monkeypatch)
    content = _content("priority_goals", {
        "goal_index": 2,
        "goal_label": "ship feature X",
        "goal_key": "feature_x",
        "hours_stale": 12,
    })
    plan = WorkspacePlanner().propose_plan(content, BUDGET_OK)

    assert len(plan) == 1
    step = plan[0]
    assert step["action"] == "advance_goal"
    assert "2" in step["description"]
    assert "ship feature X" in step["description"]
    assert step["context"]["goal_key"] == "feature_x"
    assert step["context"]["hours_stale"] == 12


def test_infrastructure_with_no_critical_falls_back_to_simple(tmp_path, monkeypatch):
    _patch_tasks_file(tmp_path, monkeypatch)
    content = _content("infrastructure", {"n_critical": 0, "triggers": []},
                       text="all quiet on infra")
    plan = WorkspacePlanner().propose_plan(content, BUDGET_OK)

    assert len(plan) == 1
    assert plan[0]["action"] == "analyze"
    assert plan[0]["description"] == "all quiet on infra"


def test_unknown_module_falls_back_to_simple(tmp_path, monkeypatch):
    _patch_tasks_file(tmp_path, monkeypatch)
    content = _content("some_unmapped_module", {"foo": "bar"}, text="x" * 300)
    plan = WorkspacePlanner().propose_plan(content, BUDGET_OK)

    assert len(plan) == 1
    assert plan[0]["action"] == "analyze"
    # _plan_simple truncates content to 200 chars.
    assert plan[0]["description"] == ("x" * 300)[:200]
    assert plan[0]["context"] == {"foo": "bar"}


# --- pending-task queue-depth gate -------------------------------------------

def test_count_pending_tasks_is_zero_when_tasks_file_missing(tmp_path, monkeypatch):
    _patch_tasks_file(tmp_path, monkeypatch)
    planner = WorkspacePlanner()
    assert planner._count_pending_tasks() == 0


def test_count_pending_tasks_counts_own_pending_only(tmp_path, monkeypatch):
    tasks_file = _patch_tasks_file(tmp_path, monkeypatch)
    tasks_file.write_text(json.dumps({"tasks": [
        {"status": "pending", "source": "zugamind"},
        {"status": "pending", "source": "workspace_planner"},
        {"status": "done", "source": "zugamind"},          # wrong status
        {"status": "pending", "source": "some_other_tool"},  # wrong source
    ]}))
    planner = WorkspacePlanner()
    assert planner._count_pending_tasks() == 2


def test_corrupt_tasks_file_fails_closed_to_zero(tmp_path, monkeypatch):
    tasks_file = _patch_tasks_file(tmp_path, monkeypatch)
    tasks_file.write_text("{not json")
    planner = WorkspacePlanner()
    assert planner._count_pending_tasks() == 0


def test_queue_depth_gate_blocks_planning_when_at_max_pending(tmp_path, monkeypatch):
    tasks_file = _patch_tasks_file(tmp_path, monkeypatch)
    tasks_file.write_text(json.dumps({"tasks": [
        {"status": "pending", "source": "zugamind"} for _ in range(5)
    ]}))
    content = _content("infrastructure", {"n_critical": 1, "triggers": []})
    plan = WorkspacePlanner(max_pending=5).propose_plan(content, BUDGET_OK)
    assert plan == []


def test_queue_depth_gate_allows_planning_below_max_pending(tmp_path, monkeypatch):
    tasks_file = _patch_tasks_file(tmp_path, monkeypatch)
    tasks_file.write_text(json.dumps({"tasks": [
        {"status": "pending", "source": "zugamind"} for _ in range(4)
    ]}))
    content = _content("infrastructure", {"n_critical": 1, "triggers": []})
    plan = WorkspacePlanner(max_pending=5).propose_plan(content, BUDGET_OK)
    assert len(plan) == 1


# --- low-budget one-step clamp -----------------------------------------------

def test_low_budget_truncates_multi_step_plan_to_one_step(tmp_path, monkeypatch):
    _patch_tasks_file(tmp_path, monkeypatch)
    content = _content("infrastructure", {
        "n_critical": 3,
        "triggers": [{"type": "local_service_down"}],
    })
    plan = WorkspacePlanner().propose_plan(content, {"remaining": 0.10})

    assert len(plan) == 1
    assert plan[0]["action"] == "analyze"  # first step of the systemic template
    assert plan[0]["total_steps"] == 1


def test_budget_exactly_at_threshold_is_not_constrained(tmp_path, monkeypatch):
    """budget_constrained is `< 0.50`, so exactly $0.50 remaining must NOT clamp."""
    _patch_tasks_file(tmp_path, monkeypatch)
    content = _content("infrastructure", {
        "n_critical": 3,
        "triggers": [{"type": "local_service_down"}],
    })
    plan = WorkspacePlanner().propose_plan(content, {"remaining": 0.50})
    assert len(plan) == 3


def test_low_budget_does_not_affect_already_single_step_plan(tmp_path, monkeypatch):
    _patch_tasks_file(tmp_path, monkeypatch)
    content = _content("infrastructure", {
        "n_critical": 1,
        "triggers": [{"type": "local_service_down", "service": "a", "port": 1}],
    })
    plan = WorkspacePlanner().propose_plan(content, {"remaining": 0.0})
    assert len(plan) == 1


def test_missing_remaining_key_defaults_to_constrained(tmp_path, monkeypatch):
    """budget.get("remaining", 0) — an empty budget dict is treated as $0."""
    _patch_tasks_file(tmp_path, monkeypatch)
    content = _content("infrastructure", {
        "n_critical": 3,
        "triggers": [{"type": "local_service_down"}],
    })
    plan = WorkspacePlanner().propose_plan(content, {})
    assert len(plan) == 1


# --- format_plan_for_prompt ---------------------------------------------------

def test_format_plan_for_prompt_empty_plan():
    assert WorkspacePlanner().format_plan_for_prompt([]) == (
        "No plan proposed (constraints prevent planning)."
    )


def test_format_plan_for_prompt_lists_steps_and_dependencies(tmp_path, monkeypatch):
    _patch_tasks_file(tmp_path, monkeypatch)
    content = _content("infrastructure", {
        "n_critical": 3,
        "triggers": [{"type": "local_service_down"}],
    })
    plan = WorkspacePlanner().propose_plan(content, BUDGET_OK)
    text = WorkspacePlanner().format_plan_for_prompt(plan)

    assert "3 step(s)" in text
    assert "[analyze]" in text
    assert "(after step 1)" in text  # step 3's raw depends_on=1 is printed verbatim
