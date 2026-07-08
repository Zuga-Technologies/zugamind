"""
ZugaMind Workspace Modules — illustrative modules that wrap scanner output
into salience bids.

Most modules aggregate scanner triggers into a single bid per cycle. A
smaller "intrinsic" subset (here: PriorityGoalsModule, MetacognitiveModule)
bid without external scanner triggers, so the workspace is never silent
when the world is idle — without an intrinsic bidder, idle cycles would
produce only metacognitive self-monitoring, which self-fulfills into a
"stuck on metacognition" loop.

These are EXAMPLE modules meant to be read, adapted, and replaced — the
demo (examples/minimal_loop.py) registers a smaller, purely synthetic set.
Zero pip dependencies (stdlib only).
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .workspace import ThoughtType, SalienceBid, WorkspaceContent, WorkspaceModule

logger = logging.getLogger("zugamind.workspace_modules")


# =============================================================================
# MODULE: Infrastructure
# =============================================================================

class InfrastructureModule(WorkspaceModule):
    """Aggregates service-health triggers.

    Bids high when services are down or degraded; bids low when healthy.
    """
    name = "infrastructure"

    TRIGGER_TYPES = {
        "local_service_down", "local_service_up", "local_systemic_failure",
        "production_down", "production_degraded", "production_healthy",
        "system_health", "environment_health",
    }

    def generate_bid(self, context: Dict[str, Any]) -> Optional[SalienceBid]:
        if not self._triggers:
            return SalienceBid(
                source_module=self.name,
                content="All infrastructure healthy — no triggers",
                salience=0.05,
                thought_type=ThoughtType.INFRASTRUCTURE,
                emotional_valence=0.3,
                context={"triggers": [], "status": "healthy"},
            )

        critical = [t for t in self._triggers if t["type"] in
                    ("local_service_down", "local_systemic_failure", "production_down")]
        degraded = [t for t in self._triggers if t["type"] in
                    ("production_degraded", "system_health")]
        healthy = [t for t in self._triggers if t["type"] not in
                   {c["type"] for c in critical + degraded}]

        if critical:
            n_critical = len(critical)
            salience = min(0.95, 0.7 + n_critical * 0.08)
            detail_parts = [t.get("detail", t.get("service", "?")) for t in critical[:3]]
            content = f"CRITICAL: {n_critical} infrastructure issue(s) — {'; '.join(detail_parts)}"
            valence = -0.8
        elif degraded:
            salience = min(0.7, 0.4 + len(degraded) * 0.1)
            detail_parts = [t.get("detail", "?") for t in degraded[:2]]
            content = f"Degraded: {'; '.join(detail_parts)}"
            valence = -0.3
        else:
            salience = 0.1
            content = f"Infrastructure OK ({len(healthy)} checks passed)"
            valence = 0.2

        return SalienceBid(
            source_module=self.name,
            content=content,
            salience=salience,
            thought_type=ThoughtType.INFRASTRUCTURE,
            emotional_valence=valence,
            context={"triggers": self._triggers, "n_critical": len(critical),
                     "n_degraded": len(degraded)},
        )


# =============================================================================
# MODULE: Daemon / task queue
# =============================================================================

class DaemonModule(WorkspaceModule):
    """Wraps background-task-queue triggers — failures, completions."""
    name = "daemon"

    TRIGGER_TYPES = {"daemon_task_complete", "daemon_task_failed", "daemon_task_started"}

    def generate_bid(self, context: Dict[str, Any]) -> Optional[SalienceBid]:
        if not self._triggers:
            return None

        failures = [t for t in self._triggers if t["type"] == "daemon_task_failed"]
        completions = [t for t in self._triggers if t["type"] == "daemon_task_complete"]

        if failures:
            salience = min(0.9, 0.5 + len(failures) * 0.15)
            content = f"Daemon: {len(failures)} task(s) failed — {failures[0].get('detail', '?')}"
            valence = -0.6
        elif completions:
            salience = 0.3
            content = f"Daemon: {len(completions)} task(s) completed"
            valence = 0.4
        else:
            salience = 0.2
            content = f"Daemon: {len(self._triggers)} event(s)"
            valence = 0.0

        return SalienceBid(
            source_module=self.name,
            content=content,
            salience=salience,
            thought_type=ThoughtType.TASK_MANAGEMENT,
            emotional_valence=valence,
            context={"triggers": self._triggers, "n_failures": len(failures)},
        )


# =============================================================================
# MODULE: Code Changes
# =============================================================================

class CodeChangeModule(WorkspaceModule):
    """Wraps recent-code-change / git-commit triggers."""
    name = "code_changes"

    TRIGGER_TYPES = {"git_commit", "code_change", "recent_code_change"}

    def generate_bid(self, context: Dict[str, Any]) -> Optional[SalienceBid]:
        if not self._triggers:
            return None

        commits = [t for t in self._triggers if t["type"] == "git_commit"]
        code_changes = [t for t in self._triggers if t["type"] in
                        ("code_change", "recent_code_change")]

        has_issues = any("fix" in t.get("detail", "").lower() or
                         "bug" in t.get("detail", "").lower()
                         for t in self._triggers)

        if has_issues:
            salience = min(0.7, 0.4 + len(self._triggers) * 0.05)
            valence = -0.2
        elif commits:
            salience = min(0.5, 0.2 + len(commits) * 0.05)
            valence = 0.1
        else:
            salience = min(0.4, 0.2 + len(code_changes) * 0.03)
            valence = 0.0

        projects = set(t.get("project", "?") for t in self._triggers if t.get("project"))
        content = (f"Code: {len(self._triggers)} change(s) in "
                   f"{', '.join(projects) if projects else 'unknown'}")

        return SalienceBid(
            source_module=self.name,
            content=content,
            salience=salience,
            thought_type=ThoughtType.CODE_QUALITY,
            emotional_valence=valence,
            context={"triggers": self._triggers, "projects": list(projects)},
        )


# =============================================================================
# MODULE: Repo issues
# =============================================================================


class RepoIssuesModule(WorkspaceModule):
    """Wraps `repo_issue` triggers from watched GitHub repos.

    Bids high enough to reliably win a quiet cycle: a new issue filed by a
    human is almost always the most salient thing an unattended agent can
    act on. Issue titles ride into the bid content verbatim so the wake
    briefing carries them.
    """
    name = "repo_issues"

    TRIGGER_TYPES = {"repo_issue"}

    def generate_bid(self, context: Dict[str, Any]) -> Optional[SalienceBid]:
        if not self._triggers:
            return None

        urgent_words = ("bug", "crash", "error", "broken", "security", "fail")
        urgent = any(w in t.get("issue_title", "").lower()
                     for t in self._triggers for w in urgent_words)

        salience = min(0.85, 0.55 + len(self._triggers) * 0.08 + (0.08 if urgent else 0.0))
        titles = "; ".join(
            f"#{t.get('issue_number', '?')} {t.get('issue_title', '?')}"
            for t in self._triggers[:2]
        )
        repos = sorted({t.get("repo", "?") for t in self._triggers})
        content = f"{len(self._triggers)} new issue(s) on {', '.join(repos)}: {titles}"

        return SalienceBid(
            source_module=self.name,
            content=content,
            salience=salience,
            thought_type=ThoughtType.CODE_QUALITY,
            emotional_valence=-0.3 if urgent else -0.1,
            context={"triggers": self._triggers, "repos": repos},
        )


# =============================================================================
# MODULE: Knowledge
# =============================================================================

class KnowledgeModule(WorkspaceModule):
    """Wraps knowledge-base / notes-change triggers."""
    name = "knowledge"

    TRIGGER_TYPES = {"vault_change", "shared_memory_update"}

    def generate_bid(self, context: Dict[str, Any]) -> Optional[SalienceBid]:
        if not self._triggers:
            return None

        vault = [t for t in self._triggers if t["type"] == "vault_change"]
        memory = [t for t in self._triggers if t["type"] == "shared_memory_update"]

        salience = min(0.5, 0.2 + len(self._triggers) * 0.05)
        files = [t.get("file", "?") for t in self._triggers[:3]]
        content = f"Knowledge: {len(self._triggers)} update(s) — {', '.join(files)}"

        return SalienceBid(
            source_module=self.name,
            content=content,
            salience=salience,
            thought_type=ThoughtType.KNOWLEDGE,
            emotional_valence=0.1,
            context={"triggers": self._triggers, "n_vault": len(vault),
                     "n_memory": len(memory)},
        )


# =============================================================================
# MODULE: Schedule
# =============================================================================

class ScheduleModule(WorkspaceModule):
    """Wraps scheduled-job / analytics-significance triggers."""
    name = "schedule"

    TRIGGER_TYPES = {"cron_output", "analytics_significant", "category_significance"}

    def generate_bid(self, context: Dict[str, Any]) -> Optional[SalienceBid]:
        if not self._triggers:
            return None

        significant = [t for t in self._triggers if t["type"] in
                       ("analytics_significant", "category_significance")]
        cron = [t for t in self._triggers if t["type"] == "cron_output"]

        if significant:
            salience = min(0.8, 0.5 + len(significant) * 0.1)
            content = f"Analytics: {significant[0].get('detail', 'significance detected')}"
            valence = 0.3
        elif cron:
            salience = min(0.5, 0.3 + len(cron) * 0.05)
            content = f"Scheduled job: {len(cron)} output(s)"
            valence = 0.0
        else:
            salience = 0.3
            content = f"Schedule: {len(self._triggers)} event(s)"
            valence = 0.0

        return SalienceBid(
            source_module=self.name,
            content=content,
            salience=salience,
            thought_type=ThoughtType.SCHEDULE,
            emotional_valence=valence,
            context={"triggers": self._triggers},
        )


# =============================================================================
# MODULE: Metacognition (intrinsic)
# =============================================================================

class MetacognitiveModule(WorkspaceModule):
    """Synthesizes from prediction accuracy, drift, and attention health.

    Unlike other modules, this doesn't wrap a single scanner — it reads the
    workspace's own state to detect problems with the cognitive process
    itself: prediction accuracy dropping, drift elevated, attention stuck.
    """
    name = "metacognition"

    TRIGGER_TYPES: set = set()  # intrinsic — no scanner mapping

    def __init__(self):
        super().__init__()
        self._prediction_accuracy: Optional[float] = None
        self._drift: float = 0.0
        self._attention_stuck: bool = False

    def set_metacognitive_state(self, prediction_accuracy: Optional[float],
                                 drift: float, attention_stuck: bool):
        """Called by the host loop with computed metacognitive metrics."""
        self._prediction_accuracy = prediction_accuracy
        self._drift = drift
        self._attention_stuck = attention_stuck

    def generate_bid(self, context: Dict[str, Any]) -> Optional[SalienceBid]:
        concerns = []
        salience = 0.1

        if self._prediction_accuracy is not None and self._prediction_accuracy < 0.5:
            concerns.append(f"prediction accuracy low ({self._prediction_accuracy:.0%})")
            salience = max(salience, 0.4 + (0.5 - self._prediction_accuracy))

        if self._drift > 0.3:
            drift_capped = min(self._drift, 0.8)
            concerns.append(f"drift elevated ({self._drift:.2f})")
            salience = max(salience, 0.3 + drift_capped * 0.5)

        # Attention stuck — but if WE are the stuck module, yield to break the
        # loop, or metacognition would perpetually re-win on "I'm stuck".
        if self._attention_stuck:
            last_winner = context.get("last_winner_module", "")
            if last_winner == self.name:
                concerns.append("attention stuck on metacognition itself — yielding")
                salience = 0.0
            else:
                concerns.append("attention stuck on same module")
                salience = max(salience, 0.3)

        if not concerns:
            return SalienceBid(
                source_module=self.name,
                content="Metacognition: all systems nominal",
                salience=0.1,
                thought_type=ThoughtType.METACOGNITION,
                emotional_valence=0.1,
                context={"concerns": []},
            )

        salience = min(0.7, salience)
        content = f"Metacognition: {'; '.join(concerns)}"

        return SalienceBid(
            source_module=self.name,
            content=content,
            salience=salience,
            thought_type=ThoughtType.METACOGNITION,
            emotional_valence=-0.3,
            context={"concerns": concerns, "accuracy": self._prediction_accuracy,
                     "drift": self._drift},
        )


# =============================================================================
# MODULE: Priority Goals (intrinsic)
# =============================================================================

class PriorityGoalsModule(WorkspaceModule):
    """Bids on advancing the least-recently-touched priority goal.

    This is the value-driven counterweight to metacognition: without it,
    idle cycles produce only metacognition's self-monitoring voice. GOALS
    below is a small illustrative example (priority order, highest first);
    replace it with your own agent's real priority list, or load it from
    `foundation/persona/charter.md`.

    Maps to plan action "advance_goal" — see workspace_planner.py.
    """
    name = "priority_goals"

    TRIGGER_TYPES: set = set()  # intrinsic — no scanner mapping

    # Example 3-goal value spine (priority order, highest first). See
    # foundation/persona/charter.md for the human-readable version of this
    # same example persona.
    GOALS = [
        ("integrity", "System integrity and safe operation"),
        ("truthfulness", "Truthful, epistemically disciplined output"),
        ("value", "Delivering value to the operator within budget"),
    ]

    def __init__(self):
        super().__init__()
        self._goal_last_touched: Dict[str, Optional[datetime]] = {
            g[0]: None for g in self.GOALS
        }

    def set_goal_state(self, goal_last_touched: Dict[str, Optional[datetime]]):
        """Called by the host loop with per-goal recency (e.g. from an event log)."""
        for k, v in goal_last_touched.items():
            if k in self._goal_last_touched:
                self._goal_last_touched[k] = v

    def generate_bid(self, context: Dict[str, Any]) -> Optional[SalienceBid]:
        now = datetime.now()
        candidates = []
        for idx, (key, label) in enumerate(self.GOALS):
            last = self._goal_last_touched.get(key)
            hours_stale = 9999.0 if last is None else (now - last).total_seconds() / 3600.0
            priority_bonus = (len(self.GOALS) - idx) * 0.5
            score = hours_stale + priority_bonus
            candidates.append((score, idx, key, label, hours_stale))

        candidates.sort(reverse=True)
        _score, idx, key, label, hours_stale = candidates[0]

        # Salience: 0.2 baseline, scales gently with staleness, capped at 0.55
        # (below metacognition's crisis ceiling of 0.7) — but on idle cycles
        # this out-ranks metacognition's 0.1 idle baseline.
        if hours_stale > 9000:
            salience = 0.45
        else:
            salience = min(0.55, 0.2 + min(hours_stale, 12.0) * 0.025)

        content = f"Priority goal #{idx + 1} ({label}) — {hours_stale:.1f}h since touched"
        if hours_stale > 9000:
            content = f"Priority goal #{idx + 1} ({label}) — never advanced this session"

        return SalienceBid(
            source_module=self.name,
            content=content,
            salience=salience,
            thought_type=ThoughtType.METACOGNITION,
            emotional_valence=0.2,
            context={
                "goal_index": idx + 1,
                "goal_key": key,
                "goal_label": label,
                "hours_stale": round(hours_stale, 2),
                "target": key,  # stable per-goal identity for the attention schema
            },
        )

    def on_broadcast(self, content: WorkspaceContent):
        """Reset this goal's staleness clock when it wins."""
        try:
            if content and content.bid and content.bid.source_module == self.name:
                key = content.bid.context.get("goal_key")
                if key in self._goal_last_touched:
                    self._goal_last_touched[key] = datetime.now()
        except Exception as e:
            logger.debug("priority_goals on_broadcast failed: %s", e)


# =============================================================================
# FACTORY
# =============================================================================

ALL_MODULES = [
    InfrastructureModule,
    DaemonModule,
    CodeChangeModule,
    RepoIssuesModule,
    KnowledgeModule,
    ScheduleModule,
    MetacognitiveModule,
    PriorityGoalsModule,
]

# Map trigger types to modules for routing.
TRIGGER_TYPE_TO_MODULE: Dict[str, str] = {}
for _ModuleClass in ALL_MODULES:
    for _ttype in _ModuleClass.TRIGGER_TYPES:
        TRIGGER_TYPE_TO_MODULE[_ttype] = _ModuleClass.name


def create_all_modules() -> List[WorkspaceModule]:
    """Create instances of all example modules. Adapt or replace freely —
    the workspace engine (workspace.py) has no dependency on this list."""
    return [cls() for cls in ALL_MODULES]


def route_triggers_to_modules(
    triggers: List[Dict[str, Any]],
    modules: List[WorkspaceModule],
) -> None:
    """Route scanner triggers to their corresponding workspace modules.

    Each trigger's 'type' field maps to a module. Triggers that don't match
    any registered module are silently dropped from this pass (they remain
    available in the raw trigger list for any other consumer).
    """
    module_map = {m.name: m for m in modules}
    grouped: Dict[str, List[Dict[str, Any]]] = {m.name: [] for m in modules}

    for trigger in triggers:
        ttype = trigger.get("type", "")
        module_name = TRIGGER_TYPE_TO_MODULE.get(ttype)
        if module_name and module_name in grouped:
            grouped[module_name].append(trigger)

    for name, module_triggers in grouped.items():
        module_map[name].set_triggers(module_triggers)
