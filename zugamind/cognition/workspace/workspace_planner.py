"""
ZugaMind Workspace Planner — converts a workspace winner into a task plan.

Sits between the workspace (WHAT to attend to) and the action gate
(whether/how to act on it — see gates/action_gate.py). The planner proposes
HOW to act: a short, dependency-ordered sequence of steps.

Budget-gated: won't propose multi-step plans if too many tasks are already
pending, or if budget is running low.

Zero pip dependencies (stdlib only).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from .workspace import WorkspaceContent

logger = logging.getLogger("zugamind.workspace_planner")

# Where the planner looks for already-pending tasks (queue-depth gate).
# Point this at your own task queue's export/snapshot file, or ignore the
# gate entirely by passing max_pending=float("inf") to WorkspacePlanner.
TASKS_FILE = Path(__file__).resolve().parent.parent.parent / "tasks.json"


# =============================================================================
# PLAN TEMPLATES
# =============================================================================

def _plan_single_restart(content: WorkspaceContent) -> List[Dict[str, Any]]:
    """Single service down -> 1 step: restart."""
    ctx = content.bid.context
    triggers = ctx.get("triggers", [])
    service = "unknown"
    port = "?"
    for t in triggers:
        if t.get("type") == "local_service_down":
            service = t.get("service", "unknown")
            port = t.get("port", "?")
            break

    return [{
        "description": f"Restart {service} (port {port})",
        "action": "restart_service",
        "context": {"service": service, "port": port, "triggers": triggers},
    }]


def _plan_systemic_restart(content: WorkspaceContent) -> List[Dict[str, Any]]:
    """3+ services down -> 3 steps: diagnose -> restart all -> verify."""
    ctx = content.bid.context
    triggers = ctx.get("triggers", [])
    return [
        {"description": "Diagnose systemic failure — check common causes",
         "action": "analyze", "context": {"triggers": triggers, "phase": "diagnose"}},
        {"description": "Restart all affected services", "action": "restart_service",
         "context": {"service": "all", "triggers": triggers}, "depends_on": 0},
        {"description": "Verify all services recovered", "action": "analyze",
         "context": {"phase": "verify"}, "depends_on": 1},
    ]


def _plan_prod_investigation(content: WorkspaceContent) -> List[Dict[str, Any]]:
    """Production down -> 3 steps: diagnose -> fix/deploy -> verify."""
    ctx = content.bid.context
    triggers = ctx.get("triggers", [])
    endpoint = "unknown"
    for t in triggers:
        if t.get("type") == "production_down":
            endpoint = t.get("endpoint", "/")
            break
    return [
        {"description": f"Diagnose production failure at {endpoint}", "action": "analyze",
         "context": {"endpoint": endpoint, "triggers": triggers, "phase": "diagnose"}},
        {"description": "Apply fix and redeploy if needed", "action": "code",
         "context": {"endpoint": endpoint, "phase": "fix"}, "depends_on": 0},
        {"description": f"Verify production endpoint recovered", "action": "analyze",
         "context": {"endpoint": endpoint, "phase": "verify"}, "depends_on": 1},
    ]


def _plan_code_analysis(content: WorkspaceContent) -> List[Dict[str, Any]]:
    """Code with issues -> 2 steps: analyze -> fix."""
    ctx = content.bid.context
    triggers = ctx.get("triggers", [])
    projects = ctx.get("projects", [])
    return [
        {"description": f"Analyze code changes in {', '.join(projects) or 'project'}",
         "action": "analyze",
         "context": {"triggers": triggers, "projects": projects, "phase": "analyze"}},
        {"description": "Apply fixes for identified issues", "action": "code",
         "context": {"triggers": triggers, "projects": projects, "phase": "fix"},
         "depends_on": 0},
    ]


def _plan_task_retry(content: WorkspaceContent) -> List[Dict[str, Any]]:
    """Task failed -> 2 steps: analyze failure -> retry."""
    ctx = content.bid.context
    triggers = ctx.get("triggers", [])
    failed = [t for t in triggers if t.get("type") == "daemon_task_failed"]
    return [
        {"description": f"Analyze why task failed: "
                        f"{failed[0].get('detail', '?') if failed else '?'}",
         "action": "analyze", "context": {"triggers": triggers, "phase": "failure_analysis"}},
        {"description": "Retry task with adjusted approach", "action": "code",
         "context": {"triggers": triggers, "phase": "retry"}, "depends_on": 0},
    ]


def _plan_priority_goal_advance(content: WorkspaceContent) -> List[Dict[str, Any]]:
    """priority_goals winner -> 1 step: pull a piece of work that advances
    the named goal. Maps to action="advance_goal" so the action gate can
    route this as a value-aligned micro-task rather than generic analysis."""
    ctx = content.bid.context
    idx = ctx.get("goal_index", "?")
    label = ctx.get("goal_label", "unspecified")
    return [{
        "description": f"Advance priority goal #{idx} ({label})",
        "action": "advance_goal",
        "context": {
            "phase": "goal_advance",
            "goal_index": idx,
            "goal_key": ctx.get("goal_key"),
            "goal_label": label,
            "hours_stale": ctx.get("hours_stale"),
        },
    }]


def _plan_simple(content: WorkspaceContent) -> List[Dict[str, Any]]:
    """Default: single-step plan — just surface what the winner said."""
    return [{
        "description": content.content[:200],
        "action": "analyze",
        "context": content.bid.context,
    }]


# =============================================================================
# PLANNER
# =============================================================================

class WorkspacePlanner:
    """Converts a workspace winner into an actionable task sequence.

    Deterministic — pattern-matches on the winning module + trigger context
    to pick a plan template. No LLM calls (that happens downstream, in the
    action gate, only for the winning step's actual execution).
    """

    def __init__(self, max_pending: int = 5):
        self.max_pending = max_pending

    def propose_plan(self, content: WorkspaceContent, budget: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Generate a task plan from the workspace winner.

        Returns [] if budget or queue constraints prevent planning this cycle.
        """
        budget_constrained = budget.get("remaining", 0) < 0.50

        pending_count = self._count_pending_tasks()
        if pending_count >= self.max_pending:
            logger.info("[Planner] Too many pending tasks (%d >= %d) — skipping plan",
                        pending_count, self.max_pending)
            return []

        plan = self._select_plan(content)

        if budget_constrained and len(plan) > 1:
            logger.info("[Planner] Budget constrained ($%.2f) — truncating to 1 step",
                        budget.get("remaining", 0))
            plan = plan[:1]

        for i, step in enumerate(plan):
            step["step_index"] = i
            step["total_steps"] = len(plan)
            step["workspace_module"] = content.source_module
            step["workspace_salience"] = round(content.salience, 3)
            step["planned_at"] = datetime.now().isoformat()

        return plan

    def _select_plan(self, content: WorkspaceContent) -> List[Dict[str, Any]]:
        module = content.source_module
        ctx = content.bid.context

        if module == "infrastructure":
            n_critical = ctx.get("n_critical", 0)
            triggers = ctx.get("triggers", [])
            if any(t.get("type") == "production_down" for t in triggers):
                return _plan_prod_investigation(content)
            if n_critical >= 3:
                return _plan_systemic_restart(content)
            if n_critical >= 1:
                return _plan_single_restart(content)
            return _plan_simple(content)

        if module == "daemon":
            if ctx.get("n_failures", 0) > 0:
                return _plan_task_retry(content)
            return _plan_simple(content)

        if module == "code_changes":
            return _plan_code_analysis(content)

        if module == "priority_goals":
            return _plan_priority_goal_advance(content)

        return _plan_simple(content)

    # The queue gate exists so the planner doesn't flood its own downstream
    # queue with self-injected work — only tasks this planner itself created
    # count against its cap (a real deployment integrating a shared queue
    # should filter by its own "source" tag the same way).
    _OWN_SOURCES = ("zugamind", "workspace_planner")

    def _count_pending_tasks(self) -> int:
        if not TASKS_FILE.exists():
            return 0
        try:
            data = json.loads(TASKS_FILE.read_text())
            tasks = data.get("tasks", [])
            return sum(
                1 for t in tasks
                if t.get("status") == "pending" and t.get("source") in self._OWN_SOURCES
            )
        except (json.JSONDecodeError, OSError):
            return 0

    def format_plan_for_prompt(self, plan: List[Dict[str, Any]]) -> str:
        """Format a plan as text for inclusion in an LLM prompt."""
        if not plan:
            return "No plan proposed (constraints prevent planning)."
        lines = [f"Proposed plan ({len(plan)} step(s)):"]
        for step in plan:
            dep = f" (after step {step['depends_on']})" if "depends_on" in step else ""
            lines.append(f"  {step['step_index'] + 1}. [{step['action']}] {step['description']}{dep}")
        return "\n".join(lines)
