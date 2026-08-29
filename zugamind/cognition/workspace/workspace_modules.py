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
repo-root demo (demo.py) registers all of them via create_all_modules() and
feeds them synthetic scanner triggers. Zero pip dependencies (stdlib only).
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from foundation.config import ENGINE_DIR
from foundation.fs import atomic_write_text

from .workspace import ThoughtType, SalienceBid, WorkspaceContent, WorkspaceModule

logger = logging.getLogger("zugamind.workspace_modules")


# Trigger fields are produced by scanners, some of them third-party. A wrong
# type in ONE field (issue_title None, detail None, relevance "0.7") used to
# raise inside generate_bid, and Workspace._gather_bids swallows that — so
# the whole module had NO bid that cycle and the real event vanished
# silently (audit 2026-08-28). Coerce at the edge instead.
def _ttype(t: Any) -> str:
    return str(t.get("type", "")) if isinstance(t, dict) else ""


def _str(t: Any, key: str, default: str = "") -> str:
    v = t.get(key) if isinstance(t, dict) else None
    if isinstance(v, str):
        return v
    if v is None:
        return default
    logger.debug("trigger field %r is %s, not str — coerced", key, type(v).__name__)
    return str(v)


# Bid content becomes the wake briefing. Only WorldSignals capped it; a
# 50,000-char `detail` on any other trigger became a 50,000-char briefing.
_CONTENT_CLIP = 200


def _clip(s: str, n: int = _CONTENT_CLIP) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _num(t: Any, key: str, default: float = 0.0) -> float:
    """A float, or `default`. Malformed input ("95%", True) degrades to the
    default — for urgency that means "calm", which keeps a malformed critical
    out of the alarm lane; the debug line is how you find that out."""
    v = t.get(key) if isinstance(t, dict) else None
    if isinstance(v, bool):
        logger.debug("trigger field %r is a bool — treated as %r", key, default)
        return default
    if isinstance(v, (int, float)):
        return float(v)
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        logger.debug("trigger field %r=%r is not numeric — treated as %r", key, v, default)
        return default


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

        critical = [t for t in self._triggers if _ttype(t) in
                    ("local_service_down", "local_systemic_failure", "production_down")]
        degraded = [t for t in self._triggers if _ttype(t) in
                    ("production_degraded", "system_health")]
        healthy = [t for t in self._triggers if _ttype(t) not in
                   {_ttype(c) for c in critical + degraded}]

        if critical:
            n_critical = len(critical)
            salience = min(0.95, 0.7 + n_critical * 0.08)
            detail_parts = [_clip(_str(t, "detail") or _str(t, "service", "?")) for t in critical[:3]]
            content = f"CRITICAL: {n_critical} infrastructure issue(s) — {'; '.join(detail_parts)}"
            valence = -0.8
        elif degraded:
            salience = min(0.7, 0.4 + len(degraded) * 0.1)
            detail_parts = [_clip(_str(t, "detail", "?")) for t in degraded[:2]]
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

        failures = [t for t in self._triggers if _ttype(t) == "daemon_task_failed"]
        completions = [t for t in self._triggers if _ttype(t) == "daemon_task_complete"]

        if failures:
            salience = min(0.9, 0.5 + len(failures) * 0.15)
            content = f"Daemon: {len(failures)} task(s) failed — {_clip(_str(failures[0], 'detail', '?'))}"
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

# Whole words only: "BugaBot", "prefix", "debugger" are not bug reports.
_ISSUE_WORD_RE = re.compile(r"\b(fix|fixes|fixed|bug|bugs|bugfix)\b", re.IGNORECASE)


class CodeChangeModule(WorkspaceModule):
    """Wraps recent-code-change / git-commit triggers."""
    name = "code_changes"

    TRIGGER_TYPES = {"git_commit", "code_change", "recent_code_change"}

    def generate_bid(self, context: Dict[str, Any]) -> Optional[SalienceBid]:
        if not self._triggers:
            return None

        commits = [t for t in self._triggers if _ttype(t) == "git_commit"]
        code_changes = [t for t in self._triggers if _ttype(t) == "code_change"]
        # "recent_code_change" is a misnomer left over from reusing this
        # module's slot for live Claude Code SESSION activity (tool calls,
        # reads, edits) — not necessarily a file edit. Session activity in a
        # read-only review looks identical to a real edit unless labeled
        # separately (found 2026-07-17: Buga correctly flagged "Code: N
        # change(s)" as misleading when nothing was actually edited).
        session_activity = [t for t in self._triggers if _ttype(t) == "recent_code_change"]

        # Only real file/commit events can claim "this looks like a fix" —
        # session activity is raw transcript text, and matching it on the
        # substrings "fix"/"bug" promoted pure chatter onto the high branch
        # (0.65+ vs 0.35), the one branch that clears a wake floor. Two of
        # ZugaMind's first production wakes were spent this way: one on a
        # branch name (`fix/shadow-measures-candidate`) quoted inside a chat
        # message, one on the path `...\BugaBot\music.js` — "BugaBot" contains
        # "bug". Word boundaries, and never session_activity (2026-08-16).
        has_issues = any(_ISSUE_WORD_RE.search(_str(t, "detail"))
                         for t in (commits + code_changes))

        if has_issues:
            salience = min(0.7, 0.4 + len(self._triggers) * 0.05)
            valence = -0.2
        elif commits:
            salience = min(0.5, 0.2 + len(commits) * 0.05)
            valence = 0.1
        else:
            salience = min(0.4, 0.2 + len(code_changes) * 0.03 + len(session_activity) * 0.03)
            valence = 0.0

        projects = set(_str(t, "project") for t in self._triggers if _str(t, "project"))
        proj_str = _clip(', '.join(sorted(projects))) if projects else 'unknown'
        if session_activity and not commits and not code_changes:
            content = (f"Session activity: {len(session_activity)} update(s) in "
                       f"{proj_str} (Claude Code working — not necessarily a file edit)")
        else:
            content = (f"Code: {len(self._triggers)} change(s) in {proj_str}")

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
        urgent = any(w in _str(t, "issue_title").lower()
                     for t in self._triggers for w in urgent_words)

        # Floor of 0.7: an untriaged human-filed issue must reliably beat the
        # ambient modules (priority_goals idles near 0.5); under salience^4
        # weighting 0.55 lost the draw ~30% of cycles, observed in rehearsal.
        salience = min(0.9, 0.7 + len(self._triggers) * 0.05 + (0.08 if urgent else 0.0))
        titles = "; ".join(
            f"#{_str(t, 'issue_number', '?')} {_clip(_str(t, 'issue_title', '?'))}"
            for t in self._triggers[:2]
        )
        repos = sorted({_str(t, "repo", "?") for t in self._triggers})
        content = f"{len(self._triggers)} new issue(s) on {_clip(', '.join(repos))}: {titles}"

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

        vault = [t for t in self._triggers if _ttype(t) == "vault_change"]
        memory = [t for t in self._triggers if _ttype(t) == "shared_memory_update"]

        salience = min(0.5, 0.2 + len(self._triggers) * 0.05)
        files = [_clip(_str(t, "file", "?"), 80) for t in self._triggers[:3]]
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

        significant = [t for t in self._triggers if _ttype(t) in
                       ("analytics_significant", "category_significance")]
        cron = [t for t in self._triggers if _ttype(t) == "cron_output"]

        if significant:
            salience = min(0.8, 0.5 + len(significant) * 0.1)
            content = f"Analytics: {_clip(_str(significant[0], 'detail', 'significance detected'))}"
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
        # The workspace stamps `attention_stuck` and `last_winner_module`
        # into every cycle context itself (2026-08-28) — before that, nothing
        # in the repo called set_metacognitive_state or passed
        # last_winner_module, so this branch was unreachable and the module
        # was a constant 0.1 bidder in production.
        if self._attention_stuck or bool(context.get("attention_stuck")):
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

    # Persisted so a process restart between wakes doesn't wipe every goal's
    # staleness clock back to "never touched" — a bug that pinned hours_stale
    # at the 9999.0 sentinel forever and fired a permanent 0.45-salience
    # noise wake every idle cycle (diagnosed in zugamind-daemon/wake-notes.md
    # 2026-07-12/13, three separate wakes, never fixed until now).
    STATE_FILE: Path = ENGINE_DIR / "priority_goals_state.json"

    # Priority breaks TIES between equally-stale goals; it must never outweigh
    # real staleness. It used to be 0.5 per rank, measured against HOURS of
    # staleness — so once every goal had been touched once, goal #1 won
    # every cycle unless another goal fell more than 30 real minutes behind,
    # which never happens at a 7-minute cadence (500-cycle measurement,
    # 2026-08-28: 283 of 285 priority_goals wins went to goal #1). The
    # module's stated purpose — advance the LEAST-RECENTLY-TOUCHED goal —
    # was silently defeated. 0.001 h = 3.6 s per rank.
    PRIORITY_TIEBREAK_HOURS = 0.001

    def __init__(self, now_fn=None):
        super().__init__()
        self._goal_last_touched: Dict[str, Optional[datetime]] = {
            g[0]: None for g in self.GOALS
        }
        # Injectable clock: staleness feeds salience, so a real datetime.now()
        # here leaks wall-clock into a DECISION — two identical cycles seconds
        # apart bid 0.201250 vs 0.201299. A replay harness can freeze it.
        self._now_fn = now_fn or datetime.now
        self._load_state()

    @staticmethod
    def _parse_touch(iso: Any) -> Optional[datetime]:
        """A persisted timestamp as a NAIVE LOCAL datetime — the same kind
        `generate_bid` subtracts it from. An offset-aware value in the file
        (hand-edited, or written by a future version) used to raise
        "can't subtract offset-naive and offset-aware" inside generate_bid,
        which the engine swallows — and since the value was persisted, the
        module lost its bid EVERY cycle until someone edited the JSON
        (audit 2026-08-28)."""
        if not isinstance(iso, str) or not iso:
            return None
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt

    def _load_state(self) -> None:
        try:
            if not self.STATE_FILE.exists():
                return
            raw = json.loads(self.STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001 — a corrupt state file must not crash the caller
            logger.warning("priority_goals state load failed (non-fatal): %s", e)
            return
        if not isinstance(raw, dict):
            logger.warning("priority_goals state is not an object; ignoring")
            return
        for key, iso in raw.items():
            if key not in self._goal_last_touched:
                continue
            try:  # one bad key must not lose the others
                self._goal_last_touched[key] = self._parse_touch(iso)
            except Exception as e:  # noqa: BLE001
                logger.warning("priority_goals: ignoring bad timestamp for %r: %s", key, e)

    def _save_state(self) -> None:
        try:
            self.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            raw = {k: (v.isoformat() if v else None) for k, v in self._goal_last_touched.items()}
            atomic_write_text(self.STATE_FILE, json.dumps(raw, indent=2))
        except Exception as e:  # noqa: BLE001 — persistence is best-effort
            logger.warning("priority_goals state save failed (non-fatal): %s", e)

    def set_goal_state(self, goal_last_touched: Dict[str, Optional[datetime]]):
        """Called by the host loop with per-goal recency (e.g. from an event log)."""
        for k, v in goal_last_touched.items():
            if k in self._goal_last_touched:
                self._goal_last_touched[k] = v
        self._save_state()

    def generate_bid(self, context: Dict[str, Any]) -> Optional[SalienceBid]:
        now = self._now_fn()
        candidates = []
        for idx, (key, label) in enumerate(self.GOALS):
            last = self._goal_last_touched.get(key)
            hours_stale = 9999.0 if last is None else (now - last).total_seconds() / 3600.0
            priority_bonus = (len(self.GOALS) - idx) * self.PRIORITY_TIEBREAK_HOURS
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
        """Reset this goal's staleness clock when it wins. Persisted
        immediately — see STATE_FILE — so it survives a process restart
        before the next wake."""
        try:
            if content and content.bid and content.bid.source_module == self.name:
                key = content.bid.context.get("goal_key")
                if key in self._goal_last_touched:
                    self._goal_last_touched[key] = self._now_fn()
                    self._save_state()
        except Exception as e:
            logger.debug("priority_goals on_broadcast failed: %s", e)


# =============================================================================
# MODULE: World signals (external)
# =============================================================================

class WorldSignalsModule(WorkspaceModule):
    """Aggregates outward-looking triggers — the world scanners' voice.

    Before this module existed, none of the world scanners' trigger types
    (hackernews_story, reddit_ai_post, ...) appeared in any TRIGGER_TYPES
    set, so route_triggers_to_modules() silently dropped every external
    signal before the bid pass: the engine shipped with eyes that were
    never wired to the brain. This module is the socket they plug into.

    Salience comes from the single strongest trigger, never from volume —
    50 triggers can't shout 50x louder than one. Capped below alarm
    territory: world news is interesting, not urgent. (A genuinely urgent
    trigger — urgency >= 0.9 — still reaches the alarm lane on its own,
    which reads trigger urgency directly, not bid salience.)

    Deployments injecting custom scanners can extend routing without a
    fork via ZUGAMIND_WORLD_SIGNAL_EXTRA_TYPES (comma-separated trigger
    types), read once at import time — set it before importing the runner.
    """
    name = "world_signals"

    TRIGGER_TYPES = {
        "hackernews_story", "reddit_ai_post", "ai_lab_research",
        "repo_star_delta", "repo_fork", "repo_release",
        "reach_web_update", "reach_search_result",
    } | {
        t.strip()
        for t in os.environ.get("ZUGAMIND_WORLD_SIGNAL_EXTRA_TYPES", "").split(",")
        if t.strip()
    }

    _SALIENCE_CAP = 0.75  # below RepoIssues' 0.7 floor + margin; never outshouts ops
    _BASE = 0.25

    def generate_bid(self, context: Dict[str, Any]) -> Optional[SalienceBid]:
        if not self._triggers:
            return None  # a quiet world is healthy — no bid, not a low bid

        best = max(self._triggers,
                   key=lambda t: _num(t, "relevance") + _num(t, "urgency"))
        salience = min(
            self._SALIENCE_CAP,
            self._BASE
            + 0.4 * _num(best, "relevance")
            + 0.2 * _num(best, "urgency"),
        )

        others = len(self._triggers) - 1
        content = _str(best, "detail", "external signal")[:200]
        # The alarm lane reads bid.context["triggers"] for urgency >= 0.9.
        # Without this key a critical external signal could NEVER take the
        # lane, contrary to what this docstring promised (audit 2026-08-28).
        # Carry the strongest trigger plus every critical one, capped, so the
        # lane sees them without the bid dragging the whole batch along.
        critical = [t for t in self._triggers if _num(t, "urgency") >= 0.9 and t is not best]
        lane_triggers = [best] + critical[:4]
        if others:
            content += f" (+{others} more external signal{'s' if others > 1 else ''})"

        return SalienceBid(
            source_module=self.name,
            content=content,
            salience=salience,
            thought_type=ThoughtType.EXTERNAL_SIGNAL,
            context={
                "trigger_count": len(self._triggers),
                "top_type": _ttype(best) or None,
                "top_url": _str(best, "url") or _str(best, "link") or None,
                "types": sorted({_ttype(t) or "?" for t in self._triggers}),
                "triggers": lane_triggers,
            },
        )


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
    WorldSignalsModule,
]

# Map trigger types to modules for routing.
TRIGGER_TYPE_TO_MODULE: Dict[str, str] = {}
for _ModuleClass in ALL_MODULES:
    for _trigger_type in _ModuleClass.TRIGGER_TYPES:
        TRIGGER_TYPE_TO_MODULE[_trigger_type] = _ModuleClass.name


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

    # Route by the modules actually passed in, not the import-time map: a
    # module registered later (module_helpers.self_register, a deployment's
    # own list) used to be routed NOTHING because TRIGGER_TYPE_TO_MODULE was
    # frozen when this file was imported (audit 2026-08-28). Two modules
    # claiming one type is a configuration error — say so, first wins.
    # Tie-break by ALL_MODULES position, not by the caller's list order:
    # "first wins" must mean the same module no matter how a caller sorted
    # or filtered its list (a reordered list used to flip the winner
    # silently). Classes not in ALL_MODULES rank after it, in encounter order.
    def _rank(m: WorkspaceModule) -> int:
        try:
            return ALL_MODULES.index(type(m))
        except ValueError:
            return len(ALL_MODULES) + modules.index(m)

    routing: Dict[str, str] = {}
    for m in sorted(modules, key=_rank):
        for ttype in getattr(m, "TRIGGER_TYPES", ()) or ():
            if ttype in routing and routing[ttype] != m.name:
                logger.warning("trigger type %r claimed by both %r and %r — routing to %r "
                               "(earlier in ALL_MODULES)", ttype, routing[ttype], m.name, routing[ttype])
                continue
            routing[ttype] = m.name

    for trigger in triggers:
        ttype = _ttype(trigger)
        module_name = routing.get(ttype) or TRIGGER_TYPE_TO_MODULE.get(ttype)
        if module_name and module_name in grouped:
            grouped[module_name].append(trigger)

    for name, module_triggers in grouped.items():
        module_map[name].set_triggers(module_triggers)
