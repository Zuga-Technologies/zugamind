"""ZugaMind stream runner — the always-on cognition loop.

An agent harness (Claude Code, OpenClaw, Hermes, Codex CLI, ...) is
reactive: it thinks when you prompt it, then forgets. This runner is the
always-on complement — it perceives via scanners, holds continuity via
continuity.journal, and WAKES the harness with a briefing when something
wins the workspace and clears the fail-closed action gate. It upgrades the
AGENT (persistence, attention, proactivity), not the underlying model.

Each cycle:
    (skipped entirely while the PAUSE kill-switch file exists at the
     package root — `touch PAUSE` halts, `rm PAUSE` resumes, no restart)
    scheduler.start_cycle()
    -> collect triggers from every registered scanner (real scanners
       package discovery, plus any extra scanner fns the caller injects);
       default world-scanner output passes habituation filtering — a
       trigger seen within HABITUATION_HOURS is damped, injected
       extra_scanners bypass the filter by design
    -> route_triggers_to_modules()
    -> workspace.run_cycle(context)                (GWT: one winner or None)
    -> scheduler.record_yield() per source
    -> cognitive state transition (see _STATE_TRANSITIONS_DOC below)
    -> journal a "cycle" event (bids summary + winner)
    -> if there's a winner AND at least one enabled harness is configured:
         if now falls in the configured quiet hours: journal
             "quiet_hours_deferred" per enabled harness and stop — no plan,
             no briefing, no gate call, no invocation. Deferred winners
             surface automatically in the next real briefing (see
             continuity.journal.build_briefing's "Deferred during quiet
             hours" section), because `state["last_wake"]` isn't advanced
             by a deferral, so the since-last-wake window keeps growing.
         else: WorkspacePlanner.propose_plan()
             -> continuity.journal.build_briefing()
             -> gates.action_gate.escalate_for_action()   (fail-closed doorway)
             -> if approved: act.command_actuator.invoke_harness() per
                enabled, configured harness (respecting --dry-run)
             -> post-hoc integrity on real invocations: work_claim checks
                each harness reply's accomplishment claims against real git
                history (journaled as "work_claim" events; the repo is the
                harness's `work_claim_repo`, else ZUGAMIND_WORK_CLAIM_REPO,
                else this checkout); value_gate scores the wake so the
                bid-modulator prior can re-weight future bids (skipped
                entirely until ZUGAMIND_VALUE_GATE_ENABLED=true — it was
                making a 90 s local-model call per real wake while "off")

Money: the monthly cap in budget.json governs the wake-DECISION call only
(the gate's cheap "should this wake?" question). The harness itself bills
through its own provider; the only limits on that hop are each harness's
max_per_hour / max_per_day. ZUGAMIND_WAKE_TIER=local makes the decision
hop free; a typo in that variable used to downgrade silently to the PAID
haiku tier — now it refuses to wake (harness_skip wake_tier_invalid).

Quiet hours never pause perception: scanners still run, the workspace
still competes, the cognitive state machine still transitions, and every
"cycle" journal event is still written — only the harness wake call itself
is suppressed. Configure via `ZUGAMIND_QUIET_HOURS="HH:MM-HH:MM"` or a
top-level `"quiet_hours"` block in the harness config file (see
act/command_actuator.py's `load_quiet_hours`); a range whose end is earlier
than its start (e.g. "23:00-07:00") correctly wraps past midnight.

State transitions (approximating "urgency" as the winning bid's salience —
the workspace's one unified attention-priority signal):
    winner, salience >= ALERT_URGENCY_THRESHOLD  -> ALERT
    winner (otherwise)                           -> FOCUSED
    no winner                                    -> RESTING
    no winner, and this is the 10th such cycle in a row -> REFLECTING instead

Fail-closed: any exception while planning/briefing/gating/invoking is
caught, journaled as a "harness_skip", and results in NO harness invocation
for that cycle — a bug in this dispatch path must never turn into an
uncontrolled call to a harness. Perception and the workspace are guarded
too: a raise there is a "cycle_error" journal line (with a `phase`), never
a dead --once or a silent daemon cycle.

Stopping the daemon: SIGINT/SIGTERM work on POSIX. On Windows every
external stop is TerminateProcess — a Python signal handler never runs —
so `zugamind stop` writes STOP_FILE (data/engine/stop.request) and the loop
polls it once a second, finishing the current cycle first. A stale request
left behind is deleted at startup. Startup journals "daemon_started" (the
previous run ended with a "shutdown" event) or "daemon_restarted" (it did
not — a kill or a crash).

CLI:
    python -m stream.runner --once
    python -m stream.runner --cycles 5
    python -m stream.runner --daemon [--interval 420] [--dry-run]

Run from the zugamind/ package directory (matching the bare-form import
convention used throughout this package — see tests/conftest.py).
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from act import command_actuator
from act import floor_calibration
from cognition.workspace import Workspace, create_all_modules, route_triggers_to_modules
from cognition.workspace.workspace_planner import WorkspacePlanner
from continuity import journal
from foundation import config as foundation_config
from foundation.failure_reason import map_local_slug
from foundation.state import load_state, save_state, transition_state
from foundation.text_format import compact_payload
from gates.action_gate import escalate_for_action
from gates.value_gate import _apply_value_prior, _enabled as _value_gate_enabled, score_action
from gates.work_claim import check_entity_grounding, check_work_claim
from scanners import (
    discover_dynamic_scanners,
    habituation_filter,
    scan_ai_labs,
    scan_hackernews,
    scan_reddit_ai,
)
from scanners.scheduler import get_scheduler

logger = logging.getLogger("zugamind.stream.runner")

ALERT_URGENCY_THRESHOLD = 0.7
REFLECT_EVERY_N_IDLE = 10
DEFAULT_DAEMON_INTERVAL_SEC = 420
# The gate's tier names (gates/action_gate.py). An unknown ZUGAMIND_WAKE_TIER
# is refused here rather than handed to the gate, which downgrades unknowns
# to paid haiku with only a log line.
_VALID_WAKE_TIERS = ("local", "haiku", "sonnet", "opus")
_QUIET_WARNED: set = set()

_STATIC_SCANNERS: Dict[str, Callable[[], List[dict]]] = {
    "scan_hackernews": scan_hackernews,
    "scan_reddit_ai": scan_reddit_ai,
    "scan_ai_labs": scan_ai_labs,
}


def _hhmm_to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_quiet_hours(quiet: Optional[Dict[str, str]], now: Optional[datetime] = None) -> bool:
    """True if `now` (default: real current local time) falls within the
    configured quiet-hours window `{"start": "HH:MM", "end": "HH:MM"}`.

    Handles a window that wraps past midnight (start > end, e.g.
    "23:00-07:00") by treating it as "active from start to 24:00, and again
    from 00:00 to end". A malformed or missing window is treated as "never
    quiet" (fail-open on the SUPPRESSION side — a broken config should not
    silently mute the whole sidecar; perception and journaling are
    unaffected either way, only the harness wake call is at stake here).
    """
    if not quiet:
        return False
    try:
        start = _hhmm_to_minutes(quiet["start"])
        end = _hhmm_to_minutes(quiet["end"])
    except Exception:
        key = repr(quiet)
        if key not in _QUIET_WARNED:  # once per distinct bad config, not once per cycle
            _QUIET_WARNED.add(key)
            logger.warning("quiet_hours %r is malformed (want {'start': 'HH:MM', 'end': 'HH:MM'}); "
                           "treating as NO quiet hours — wakes are NOT suppressed", quiet)
        return False
    if start == end:
        return False  # zero-width window — treat as "no quiet hours", not "always quiet"
    current_dt = now if now is not None else datetime.now()
    current = current_dt.hour * 60 + current_dt.minute
    if start < end:
        return start <= current < end
    return current >= start or current < end  # wraps past midnight


class StreamRunner:
    """One always-on cognition loop: perceive -> workspace -> gate -> harness.

    `extra_scanners` lets a caller (a test, a private deployment) inject
    additional `scan_*` callables without touching the scanners package —
    they always run (they're unknown to the SourceScheduler's cadence
    table, so the cadence gate — itself off by default — never suppresses
    them).

    `include_default_scanners` (default True) wires in the real, shipped
    world-scanners (scan_hackernews, scan_reddit_ai, scan_ai_labs) plus
    anything scanners.discover_dynamic_scanners() finds — the production
    default. Tests that want a fully hermetic, offline cycle (no real HTTP
    calls) should pass `include_default_scanners=False` alongside toy
    `extra_scanners`.
    """

    def __init__(
        self,
        extra_scanners: Optional[Dict[str, Callable[[], List[dict]]]] = None,
        dry_run: bool = False,
        include_default_scanners: bool = True,
        attention_health_enabled: bool = True,
    ):
        self.workspace = Workspace(attention_health_enabled=attention_health_enabled)
        # Restore the attention self-model (streaks, win counts, blind spots)
        # from the last run. AttentionSchema.to_dict/restore_from_dict had
        # existed for months with NO caller: every restart reset the mind's
        # sense of what it had been attending to (audit 2026-08-28). The
        # snapshot rides inside state.json under "attention". Restored before
        # the modules are registered; the win table is seeded per module on
        # its first valid bid, so a restore and a fresh start compose cleanly.
        # The idle-cycle counter rides along: it decides the every-10th-idle
        # REFLECTING transition, and in memory only it reset on every restart
        # (a --once-per-cron deployment could never reflect at all).
        self._idle_cycles = 0
        try:
            saved = load_state()
            snapshot = saved.get("attention")
            if isinstance(snapshot, dict) and snapshot:
                self.workspace.attention_schema.restore_from_dict(snapshot)
            idle = saved.get("idle_cycles", 0)
            if isinstance(idle, (int, float)) and not isinstance(idle, bool) and idle >= 0:
                self._idle_cycles = int(idle)
        except Exception as e:  # noqa: BLE001 — a bad snapshot must never block startup
            logger.warning("attention snapshot not restored (%s); starting fresh", e)
        self.modules = create_all_modules()
        for m in self.modules:
            self.workspace.register_module(m)
        # Value-gate prior: re-weights bid types by whether acting on them
        # historically changed real state. Registered unconditionally but a
        # byte-identical no-op until ZUGAMIND_VALUE_GATE_ENABLED=true —
        # see gates/value_gate.py ("Ships DARK").
        self.workspace.register_modulator(
            lambda bids, _ctx: _apply_value_prior(bids)[0]
        )
        self.planner = WorkspacePlanner()
        self.scheduler = get_scheduler()
        self.dry_run = dry_run
        self._paused_logged = False
        self._last_raw_trigger_count = 0

        self.scanners: Dict[str, Callable[[], List[dict]]] = {}
        if include_default_scanners:
            self.scanners.update(_STATIC_SCANNERS)
            try:
                self.scanners.update(discover_dynamic_scanners())
            except Exception as e:  # noqa: BLE001 — discovery failure must not block startup
                logger.debug("dynamic scanner discovery failed (non-fatal): %s", e)
        # Only the default world-scanners get habituation filtering. Injected
        # extra_scanners are the caller's own synthetic sources and bypass it
        # by design — verify_harness re-plants its canary every retry cycle.
        self._habituated = set(self.scanners.keys())
        if extra_scanners:
            self.scanners.update(extra_scanners)
            self._habituated -= set(extra_scanners.keys())

    # -- perception ------------------------------------------------------

    def _collect_triggers(self) -> List[dict]:
        self.scheduler.start_cycle()
        due_names = {s.name for s in self.scheduler.due_sources()}
        known_names = set(self.scheduler.specs.keys())

        triggers: List[dict] = []
        raw_count = 0
        for name, fn in self.scanners.items():
            if name in known_names and name not in due_names:
                continue  # cadence-gated (no-op unless the scheduler flag is on)
            self.scheduler.note_polled(name)
            try:
                found = list(fn() or [])
            except Exception as e:  # noqa: BLE001 — one bad scanner must not sink the cycle
                logger.warning("scanner %s failed (non-fatal): %s", name, e)
                found = []
            raw_count += len(found)
            if found and name in self._habituated:
                try:
                    found = habituation_filter(found)
                except Exception as e:  # noqa: BLE001 — damping is best-effort, never lossy
                    logger.warning("habituation filter failed (non-fatal, unfiltered): %s", e)
            triggers.extend(found)
            self.scheduler.record_yield(name, len(found))  # the NOVEL yield, by scheduler design
        self._last_raw_trigger_count = raw_count
        return triggers

    # -- one cycle ---------------------------------------------------------

    def run_once(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Run exactly one cycle. Returns a small summary dict.

        `now` overrides "the current time" for the quiet-hours check only
        — real callers should omit it; tests use it to exercise a fixed
        clock deterministically.
        """
        # Kill-switch: `touch PAUSE` at the package root halts the whole
        # cycle (perception included — unlike quiet hours, which only
        # suppress the wake call). `rm PAUSE` resumes on the next cycle,
        # no restart needed. Journaled once per pause/resume transition.
        if foundation_config.PAUSE_FILE.exists():
            if not self._paused_logged:
                journal.append_event("paused", {"pause_file": str(foundation_config.PAUSE_FILE)})
                self._paused_logged = True
            return {"paused": True, "trigger_count": 0, "winner": None,
                    "state": "PAUSED", "harness_results": []}
        if self._paused_logged:
            journal.append_event("resumed", {})
            self._paused_logged = False

        cycle_error: Optional[str] = None
        try:
            triggers = self._collect_triggers()
            route_triggers_to_modules(triggers, self.modules)
        except Exception as e:  # noqa: BLE001
            # Perception ran OUTSIDE any guard until 2026-08-29: a raise in
            # the router or the scheduler killed --once with no journal line,
            # and the daemon swallowed it with a log line only.
            triggers = []
            cycle_error = self._journal_cycle_error("perception", e, 0)

        try:
            content = self.workspace.run_cycle({"trigger_count": len(triggers)})
        except Exception as e:  # noqa: BLE001 — one bad cycle must be a journal line, not a dead daemon
            # Before 2026-08-28 this propagated: --once/--cycles died and
            # --daemon swallowed it with NO journal event for the cycle.
            cycle_error = self._journal_cycle_error("workspace", e, len(triggers))
            content = None
        winner_dict = content.to_dict() if content else None

        state = self._transition_state(winner_dict)
        state["attention"] = self.workspace.attention_schema.to_dict()
        state["idle_cycles"] = self._idle_cycles
        self._save_state_safe(state)

        journal.append_event("cycle", {
            # trigger_count is what survived habituation (what the mind saw);
            # raw_trigger_count is what the scanners actually found. A busy,
            # repeat-heavy day and a quiet one look identical on the first.
            "trigger_count": len(triggers),
            "raw_trigger_count": self._last_raw_trigger_count,
            "bids": self.workspace.get_stats()["last_bids"],
            # A bounded copy: the in-memory winner (with its full triggers)
            # still drives the briefing and the gate; the journal gets a
            # summary, not 25 KB of trigger text per cycle.
            "winner": compact_payload(winner_dict) if winner_dict else None,
            "state": state.get("state"),
        })

        result: Dict[str, Any] = {
            "trigger_count": len(triggers),
            "raw_trigger_count": self._last_raw_trigger_count,
            "winner": winner_dict,
            "state": state.get("state"),
            "harness_results": [],
            "cycle_error": cycle_error,
        }

        if content is not None:
            result["harness_results"] = self._dispatch_to_harnesses(content, winner_dict, state, now)

        return result

    @staticmethod
    def _journal_cycle_error(phase: str, e: BaseException, trigger_count: int) -> str:
        reason = f"cycle_error:{e}"
        logger.error("%s failed (%s); treating as no winner", phase, e)
        journal.append_event("cycle_error", {
            "phase": phase, "trigger_count": trigger_count, "error": str(e)[:300],
            "failure_reason": map_local_slug(reason),
        })
        return str(e)[:300]

    @staticmethod
    def _save_state_safe(state: Dict[str, Any]) -> bool:
        """save_state raised PermissionError out of run_once when a hand-run
        --once and the daemon replaced state.json at the same instant on
        Windows (proved 2026-08-29; fs.py now retries, this is the backstop).
        A lost save is a journal line, never a dead cycle."""
        try:
            save_state(state)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("state.json not saved (%s); continuing", e)
            try:
                journal.append_event("state_persist_failed", {"error": str(e)[:200]})
            except Exception:  # noqa: BLE001
                pass
            return False

    def _transition_state(self, winner_dict: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        state = load_state()
        if winner_dict is None:
            self._idle_cycles += 1
            if self._idle_cycles % REFLECT_EVERY_N_IDLE == 0:
                return transition_state(state, "REFLECTING",
                                         f"{self._idle_cycles}th consecutive idle cycle")
            return transition_state(state, "RESTING", "no workspace winner this cycle")

        self._idle_cycles = 0
        urgency = winner_dict.get("salience", 0.0)
        ctx = winner_dict.get("context") or {}
        lane = bool(ctx.get("alarm_lane")) if isinstance(ctx, dict) else False
        # Judge ALERT on min(bid, modulated), the same clamp the wake floor
        # uses: a multiplier may lower a bid but never buy an alarm. Live on
        # 2026-08-29 01:35 a 0.51 world_signals bid ("Privacy choices" on a
        # watched news page) was boosted to 0.785, flipped the state to
        # ALERT and journaled an "alarm" — then the wake gate refused it on
        # the raw 0.51. The alarm lane is judged separately (below).
        raw = ctx.get("raw_salience") if isinstance(ctx, dict) else None
        if isinstance(urgency, (int, float)) and isinstance(raw, (int, float)) and not isinstance(raw, bool):
            urgency = min(urgency, raw)
        hot = isinstance(urgency, (int, float)) and urgency >= ALERT_URGENCY_THRESHOLD
        # The alarm lane rescues a critical whose MODULE is dampened (its
        # salience can sit at 0.15). Judging ALERT on that number left the
        # rescued critical in FOCUSED with no "alarm" event and a briefing
        # that read like routine chatter — the one consumer of the urgency
        # signal never received it (audit 2026-08-29).
        if hot or lane:
            reason = (f"winner urgency {urgency:.2f} >= {ALERT_URGENCY_THRESHOLD}" if hot
                      else f"alarm-lane critical (module salience dampened to {urgency:.2f})")
            state = transition_state(state, "ALERT", reason)
            journal.append_event("alarm", {
                "detail": f"{winner_dict['source_module']}: {str(winner_dict['content'])[:200]}",
                "urgency": urgency,
                "alarm_lane": lane,
            })
            return state
        return transition_state(state, "FOCUSED", f"winner from {winner_dict['source_module']}")

    # -- winner -> plan -> briefing -> gate -> harness --------------------

    @staticmethod
    def _harness_filter_reason(hc: Dict[str, Any], winner_dict: Dict[str, Any]) -> Optional[str]:
        """Why this harness's wake filter refuses this winner; None = it
        wants it. Fail-closed on a floor it cannot read: the config loader
        rejects those, but this is a public seam fed hand-built dicts, and
        "a floor I don't understand" must never mean "no floor"."""
        modules = hc.get("wake_modules")
        if isinstance(modules, list) and modules:
            if winner_dict.get("source_module") not in modules:
                return f"module_filter:{winner_dict.get('source_module')} not in {modules}"
        # An alarm-lane winner bypasses the salience floor: its salience may
        # be dampened to near-zero by the attention schema (that dampening is
        # about its module's CHATTER), but the lane already decided this
        # specific critical must surface. EXP-003 measured the cost of not
        # doing this: the dominant source's own genuine alert won selection
        # and then died right here at the floor (domreal_recall 0.2).
        if (winner_dict.get("context") or {}).get("alarm_lane"):
            return None
        floor = hc.get("wake_min_salience")
        # Which number the floor judges. Default is the post-modulation
        # `salience` — a static `wake_min_salience: 0.6` in a harness config
        # was written against that number, so changing its meaning underneath
        # it would silently re-tune every hand-set floor in the fleet.
        basis = "modulated"
        calibrated = floor == "calibrate"
        if calibrated:
            # Opt-in self-calibrating floor (issue #12) — resolves to the
            # learned floor once calibrated, WARMUP_FLOOR (0.35, today's old
            # static default) until then. Never more permissive than the
            # shipped default while still learning. See act/floor_calibration.py.
            floor, basis = floor_calibration.resolve_gate(hc.get("name", ""))
        if floor is None:
            return None
        if isinstance(floor, bool) or not isinstance(floor, (int, float)):
            # True/"Calibrate"/"0.6": the loader raises on these; a caller that
            # skipped the loader used to get NO floor (wake on everything).
            return f"floor_invalid:{floor!r}"
        if isinstance(floor, (int, float)):
            salience = winner_dict.get("salience", 0.0)
            raw = (winner_dict.get("context") or {}).get("raw_salience")
            if calibrated and isinstance(raw, (int, float)):
                # ONE CLAMP, BOTH BASES: min(modulated, raw). The principle is
                # "a multiplier may LOWER a bid but never buy a wake", and it
                # takes both halves of this min() to hold it.
                #
                # raw guards the BOOST half. The attention schema's
                # monopoly-breaking multipliers exist to share attention
                # inside the mind; they were never meant to authorise spending
                # a real session. Measured 2026-08-17: a 0.5164 bid cleared a
                # 0.655 floor as 0.6816 on boosts alone.
                #
                # modulated guards the DAMPENING half, and judging raw ALONE
                # threw it away — the "becomes a no-op once basis flips to
                # raw" note this comment replaced was wrong about that.
                # Dampening (diversity cap, streak penalty) is the mind's own
                # "this module is monopolising, quiet it", and it was the only
                # thing standing between a repeat source and a session.
                # Measured 2026-08-18: world_signals bid raw 0.670, was damped
                # to 0.250 — the hardest damping in the whole 90-sample
                # raw-stamped record — and the gate waved it through on 0.670
                # vs a 0.600 floor, for a watched page whose newest headline
                # was four days old.
                #
                # Cross-basis is safe in this direction only. A raw-fitted
                # floor must never judge modulated on its own (modulated runs
                # systematically higher, so the floor would go permissive —
                # the trap resolve_gate() exists to avoid); min() can only
                # ever subtract, so it makes the gate stricter, never looser.
                #
                # Missing raw is judged on `salience` rather than waved
                # through: an unstamped bid must not become a free pass. A
                # hand-set static floor (not `calibrate`) keeps judging
                # modulated untouched — it was written against that number.
                salience = min(salience, raw)
                basis = "min(bid, modulated)"
            if not isinstance(salience, (int, float)) or isinstance(salience, bool):
                return f"salience_invalid:{salience!r}"
            if salience < floor:
                return f"floor:{basis} {salience:.3f} < {floor:.3f}"
        return None

    @staticmethod
    def _harness_wants(hc: Dict[str, Any], winner_dict: Dict[str, Any]) -> bool:
        """Apply a harness config's optional wake filter to this winner."""
        return StreamRunner._harness_filter_reason(hc, winner_dict) is None

    def _dispatch_to_harnesses(
        self,
        content: Any,
        winner_dict: Dict[str, Any],
        state: Dict[str, Any],
        now: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Fail-closed: any exception here means NO harness invocation."""
        try:
            enabled_configs = [
                hc for hc in command_actuator.load_harness_configs() if hc.get("enabled", True)
            ]
            if not enabled_configs:
                return []

            # Per-harness wake filter: a harness can opt to be woken only for
            # specific modules ("wake_modules": ["repo_issues"]) and/or above
            # a salience floor ("wake_min_salience": 0.6). Without a filter a
            # harness wakes for every gated winner — including ambient ones —
            # which is the heartbeat-spam failure mode this sidecar exists to
            # avoid. Observed in rehearsal: 3 wakes in 3 cycles for idle
            # priority-goal winners.
            pre_filter_configs = enabled_configs
            filter_reasons = [(hc, self._harness_filter_reason(hc, winner_dict)) for hc in enabled_configs]
            enabled_configs = [hc for hc, reason in filter_reasons if reason is None]

            # Record this cycle's winner as an ambient calibration sample for
            # any harness in "calibrate" mode — AFTER the filter decision
            # above, so this winner's own salience can only affect the floor
            # starting next cycle, never retroactively filter itself out.
            for hc in pre_filter_configs:
                floor_calibration.maybe_record_ambient_sample(hc, winner_dict)

            if not enabled_configs:
                journal.append_event("wake_filtered", {
                    "winner_module": winner_dict.get("source_module"),
                    "salience": winner_dict.get("salience"),
                    # which harness refused, and why — the event used to say only "filtered"
                    "filters": [{"harness": hc.get("name"), "reason": reason} for hc, reason in filter_reasons],
                })
                return []

            quiet = command_actuator.load_quiet_hours()
            if is_quiet_hours(quiet, now):
                compact_winner = compact_payload(winner_dict)  # 26 KB raw x N harnesses, measured 2026-08-29
                for hc in enabled_configs:
                    journal.append_event("quiet_hours_deferred", {
                        "harness": hc["name"], "winner": compact_winner,
                    })
                return []

            # The planner's budget gate ("won't propose multi-step plans if
            # budget is running low") reads budget["remaining"] in USD. This
            # used to be a hardcoded {"remaining": 10.0}, so the gate could
            # never fire regardless of real spend (audit 2026-08-28). Hand it
            # the real ledger; fall back to the old stub if it cannot load.
            try:
                from foundation.budget import load_budget  # noqa: WPS433 — lazy, test-patchable
                budget = load_budget()
            except Exception as e:  # noqa: BLE001 — planning must not die on a ledger hiccup
                logger.warning("planner budget unavailable (%s); planning unconstrained", e)
                budget = {"remaining": 10.0}
            plan = self.planner.propose_plan(content, budget)

            since_iso = state.get("last_wake")
            # A last_wake in the FUTURE (clock skew, a restored snapshot, a
            # hand-edited state.json) silently emptied every briefing until
            # wall-clock time caught up — "0 alarms" right after "prod is
            # down" was journaled. Journal it, and brief from the whole
            # journal instead.
            now_iso = journal.now_iso()
            if since_iso and str(since_iso) > now_iso:
                logger.warning("state.last_wake %s is in the future (now %s); briefing without a window", since_iso, now_iso)
                journal.append_event("last_wake_in_future", {"last_wake": since_iso, "now": now_iso})
                since_iso = None
            # Critical digest: alarms that lost this cycle's selection ride
            # along in the briefing instead of queueing for a wake slot they
            # may never get (EXP-001 acceptance finding — with more
            # concurrent alarm windows than slots, rotation alone still
            # drops whoever expires first).
            winner_module = winner_dict.get("source_module")
            # No salience condition here: a critical is a critical. The old
            # `salience >= ALARM_MIN_SALIENCE` guard excluded criticals from
            # attention-dampened modules — the same defect EXP-003 measured
            # in the lane (domreal_recall 0.2): DOMREAL couldn't win a wake
            # AND couldn't ride the digest, because both doors checked the
            # dampened module salience instead of the alert's own urgency.
            # Digest space is briefing text — bundling one more critical is
            # nearly free; silently dropping one is not.
            other_criticals = [
                {"source_module": b.source_module, "context": b.context}
                for b in self.workspace.last_cycle_bids
                if b.source_module != winner_module
                and self.workspace._is_critical(b)
            ]
            briefing = journal.build_briefing(
                since_iso, winner=winner_dict, other_criticals=other_criticals,
                harnesses=[hc.get("name", "") for hc in enabled_configs],
            )

            intent = {
                "kind": "decide",
                "summary": f"ZugaMind workspace winner: {str(winner_dict['content'])[:200]}",
                "context": {"winner": winner_dict, "plan": plan},
                "caller": "stream.runner",
            }
            # ZUGAMIND_WAKE_TIER routes the gate's wake-decision call to a
            # specific tier — "local" makes the entire idle-and-decide loop
            # model-bill-free (Ollama judges the wake, the harness is the
            # only paid hop). Unset = the gate's per-kind default.
            wake_tier = os.environ.get("ZUGAMIND_WAKE_TIER", "").strip()
            if wake_tier and wake_tier not in _VALID_WAKE_TIERS:
                # The gate downgrades an unknown tier to PAID haiku with one
                # log line — the opposite of what an operator who typed
                # "local" for a bill-free loop intended. Refuse instead;
                # `zugamind doctor` names the bad value.
                skip_reason = f"wake_tier_invalid:{wake_tier}"
                journal.append_event("harness_skip", {
                    "reason": skip_reason, "failure_reason": map_local_slug(skip_reason),
                })
                return []
            if wake_tier:
                intent["tier"] = wake_tier
            gate_result = escalate_for_action(intent, dry_run=self.dry_run)

            if not gate_result.get("ok"):
                skip_reason = gate_result.get("reason", "gate_not_ok")
                journal.append_event("harness_skip", {
                    "reason": skip_reason,
                    "failure_reason": map_local_slug(skip_reason),
                })
                return []

            # One guarded call per harness. invoke_harness documents "never
            # raises" and honours it today; if that ever slips, a raise from
            # harness A used to land in the fail-closed except below AFTER A
            # had really run — B never ran and last_wake was never advanced,
            # so the next briefing re-briefed a wake that already happened.
            harness_results: List[Dict[str, Any]] = []
            for hc in enabled_configs:
                try:
                    harness_results.append(
                        command_actuator.invoke_harness(hc, briefing, dry_run=self.dry_run))
                except Exception as e:  # noqa: BLE001 — isolate one harness's failure from the rest
                    err = f"runner_error:{e}"[:300]
                    logger.warning("harness %s raised (isolated): %s", hc.get("name"), e)
                    journal.append_event("harness_invocation", {
                        "harness": hc.get("name"), "ok": False, "error": err, "dry_run": self.dry_run,
                    })
                    harness_results.append({"harness": hc.get("name"), "ok": False,
                                            "error": err, "dry_run": self.dry_run})

            if harness_results:
                state["last_wake"] = journal.now_iso()
                self._save_state_safe(state)
                self._post_action_integrity(winner_dict, harness_results, enabled_configs)

            return harness_results
        except Exception as e:  # noqa: BLE001 — fail-closed: no gate error reaches a harness call
            logger.warning("harness dispatch failed (fail-closed, no harness invoked): %s", e)
            skip_reason = f"runner_error:{e}"
            journal.append_event("harness_skip", {
                "reason": skip_reason,
                "failure_reason": map_local_slug(skip_reason),
            })
            return []

    def _post_action_integrity(
        self, winner_dict: Dict[str, Any], harness_results: List[Dict[str, Any]],
        configs: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Post-hoc integrity layer, run AFTER real harness invocations.

        - work_claim: each real (non-dry-run) harness reply is checked for
          accomplishment claims against real git history; results with a
          claim are journaled as "work_claim" events (backed or not).
        - value_gate scoring: the wake itself is scored so the value prior
          (registered as a bid modulator in __init__) has data to re-weight
          future bids — a no-op until ZUGAMIND_VALUE_GATE_ENABLED=true.

        Everything here is best-effort and fail-open: an integrity check
        can flag, journal, and dampen, but must never affect the cycle that
        already happened or block a future one outright.
        """
        real_results = [
            hr for hr in harness_results
            if hr.get("ok") and not hr.get("dry_run")
        ]
        repo_by_harness = {hc.get("name"): hc.get("work_claim_repo") for hc in (configs or [])}
        for hr in real_results:
            try:
                text = (hr.get("stdout") or "").strip()
                if not text:
                    continue
                claim_repo = repo_by_harness.get(hr.get("harness"))
                wc = check_work_claim(text, repo_root=claim_repo)
                if wc.get("reason") != "no_work_claim":
                    journal.append_event("work_claim", {
                        "harness": hr.get("harness"),
                        "backed": wc.get("backed"),
                        "unbacked": (wc.get("unbacked") or [])[:3],
                        "reason": wc.get("reason"),
                    })
                # The verb-based check above is unbounded-leaky by design: a
                # claim with no listed verb ("ClickHouse is now in our stack")
                # passes it untouched. Entity grounding is the noun-side half of
                # the same question and has existed, tested, called by nothing,
                # since it was written — so that whole class of confabulation
                # went unrecorded (audit 2026-08-29). Advisory and journal-only,
                # like its neighbour: it flags, it never blocks.
                eg = check_entity_grounding(text, repo_root=claim_repo)
                if not eg.get("grounded", True):
                    journal.append_event("entity_grounding", {
                        "harness": hr.get("harness"),
                        "grounded": False,
                        "ungrounded": (eg.get("ungrounded") or [])[:5],
                        "reason": eg.get("reason"),
                    })
            except Exception as e:  # noqa: BLE001 — integrity is advisory, never disruptive
                logger.debug("work_claim check failed (fail-open): %s", e)
        # score_action is NOT a no-op when the value gate is off: its
        # "ambiguous" branch (action="alert" always is) asked the local model
        # a question with a 90 s timeout on every real wake (measured
        # 2026-08-29). Skip it entirely until the operator opts in.
        if real_results and _value_gate_enabled():
            try:
                ctx = winner_dict.get("context") or {}
                trigs = ctx.get("triggers") or []
                ttype = trigs[0].get("type", "") if trigs and isinstance(trigs[0], dict) else ""
                score_action(
                    source_module=winner_dict.get("source_module", ""),
                    action="alert",
                    trigger_type=ttype,
                    summary=str(winner_dict.get("content", ""))[:300],
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("value_gate scoring failed (fail-open): %s", e)

    # -- multi-cycle / daemon ----------------------------------------------

    def run_cycles(self, n: int) -> List[Dict[str, Any]]:
        return [self.run_once() for _ in range(n)]

    def _journal_startup(self) -> None:
        """"daemon_started" when the previous run ended with a "shutdown"
        event (or there is no history); "daemon_restarted" when it did not —
        a kill or a crash. Until 2026-08-29 nothing emitted either; the only
        trace of a restart was a gap between "cycle" timestamps."""
        try:
            last = journal.read_events(limit=1)
        except Exception:  # noqa: BLE001
            last = []
        payload: Dict[str, Any] = {"pid": os.getpid(), "dry_run": self.dry_run}
        if last and last[-1].get("kind") != "shutdown":
            payload["last_event_kind"] = last[-1].get("kind")
            payload["last_event_ts"] = last[-1].get("ts")
            journal.append_event("daemon_restarted", payload)
        else:
            journal.append_event("daemon_started", payload)

    def run_daemon(self, interval: int = DEFAULT_DAEMON_INTERVAL_SEC) -> None:
        """Loop `run_once()` until SIGINT/SIGTERM (POSIX) or STOP_FILE appears
        (any platform — the only path that works on Windows), then journal a
        "shutdown" event and return."""
        stop = {"flag": False, "reason": "signal"}

        def _handle_signal(signum, frame):  # noqa: ANN001 — stdlib signal handler signature
            logger.info("stream.runner received signal %s — shutting down after this cycle", signum)
            stop["flag"] = True

        try:
            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)
        except ValueError:
            pass  # not the main thread (embedded / tests): the stop file still works

        stop_file = foundation_config.STOP_FILE
        try:
            stop_file.unlink(missing_ok=True)  # a stale request must not stop a fresh daemon
        except OSError:
            pass
        self._journal_startup()

        logger.info("stream.runner daemon starting (interval=%ss, dry_run=%s)", interval, self.dry_run)
        while not stop["flag"]:
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001 — one bad cycle must not kill the daemon
                logger.warning("cycle failed (non-fatal, continuing): %s", e)
                try:
                    self._journal_cycle_error("daemon_loop", e, 0)
                except Exception:  # noqa: BLE001
                    pass
            for _ in range(max(1, interval)):
                if stop["flag"]:
                    break
                if stop_file.exists():
                    stop["flag"] = True
                    stop["reason"] = "stop_file"
                    break
                time.sleep(1)

        try:
            stop_file.unlink(missing_ok=True)
        except OSError:
            pass
        journal.append_event("shutdown", {"reason": stop["reason"]})
        logger.info("stream.runner daemon shutdown complete (%s)", stop["reason"])


# --- CLI ----------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run exactly one cycle and exit")
    mode.add_argument("--cycles", type=int, metavar="N", help="run N cycles and exit")
    mode.add_argument("--daemon", action="store_true", help="run forever until SIGINT/SIGTERM")
    parser.add_argument("--interval", type=int, default=DEFAULT_DAEMON_INTERVAL_SEC,
                        help=f"seconds between --daemon cycles (default {DEFAULT_DAEMON_INTERVAL_SEC})")
    parser.add_argument("--dry-run", action="store_true",
                        help="approve nothing for real spend and never exec a harness command")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    if args.cycles is not None and args.cycles < 0:
        parser.error("--cycles must be >= 0")
    runner = StreamRunner(dry_run=args.dry_run)

    if args.daemon:
        runner.run_daemon(interval=args.interval)
        return 0

    # `--cycles 0` used to run one cycle (falsy), `--cycles -3` ran none
    # silently, and every run exited 0 even when every cycle errored.
    n = 1 if args.cycles is None else args.cycles
    results = runner.run_cycles(n)
    for i, r in enumerate(results, 1):
        winner = r["winner"]
        summary = (f"{winner['source_module']}: {str(winner['content'])[:80]}"
                   if winner else "(no winner)")
        print(f"cycle {i}/{n} state={r['state']} triggers={r['trigger_count']} winner={summary}")
        for hr in r["harness_results"]:
            print(f"  harness[{hr.get('harness')}] ok={hr.get('ok')} dry_run={hr.get('dry_run')}")
        if r.get("cycle_error"):
            print(f"  cycle error: {r['cycle_error']}")
    return 1 if any(r.get("cycle_error") for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
