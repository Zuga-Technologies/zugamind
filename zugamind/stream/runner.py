"""ZugaMind stream runner — the always-on cognition loop.

An agent harness (Claude Code, OpenClaw, Hermes, Codex CLI, ...) is
reactive: it thinks when you prompt it, then forgets. This runner is the
always-on complement — it perceives via scanners, holds continuity via
continuity.journal, and WAKES the harness with a briefing when something
wins the workspace and clears the fail-closed action gate. It upgrades the
AGENT (persistence, attention, proactivity), not the underlying model.

Each cycle:
    scheduler.start_cycle()
    -> collect triggers from every registered scanner (real scanners
       package discovery, plus any extra scanner fns the caller injects)
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
uncontrolled call to a harness.

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
import signal
import sys
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from act import command_actuator
from cognition.workspace import Workspace, create_all_modules, route_triggers_to_modules
from cognition.workspace.workspace_planner import WorkspacePlanner
from continuity import journal
from foundation.state import load_state, save_state, transition_state
from gates.action_gate import escalate_for_action
from scanners import discover_dynamic_scanners, scan_ai_labs, scan_hackernews, scan_reddit_ai
from scanners.scheduler import get_scheduler

logger = logging.getLogger("zugamind.stream.runner")

ALERT_URGENCY_THRESHOLD = 0.7
REFLECT_EVERY_N_IDLE = 10
DEFAULT_DAEMON_INTERVAL_SEC = 420

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
    ):
        self.workspace = Workspace()
        self.modules = create_all_modules()
        for m in self.modules:
            self.workspace.register_module(m)
        self.planner = WorkspacePlanner()
        self.scheduler = get_scheduler()
        self.dry_run = dry_run
        self._idle_cycles = 0

        self.scanners: Dict[str, Callable[[], List[dict]]] = {}
        if include_default_scanners:
            self.scanners.update(_STATIC_SCANNERS)
            try:
                self.scanners.update(discover_dynamic_scanners())
            except Exception as e:  # noqa: BLE001 — discovery failure must not block startup
                logger.debug("dynamic scanner discovery failed (non-fatal): %s", e)
        if extra_scanners:
            self.scanners.update(extra_scanners)

    # -- perception ------------------------------------------------------

    def _collect_triggers(self) -> List[dict]:
        self.scheduler.start_cycle()
        due_names = {s.name for s in self.scheduler.due_sources()}
        known_names = set(self.scheduler.specs.keys())

        triggers: List[dict] = []
        for name, fn in self.scanners.items():
            if name in known_names and name not in due_names:
                continue  # cadence-gated (no-op unless the scheduler flag is on)
            self.scheduler.note_polled(name)
            try:
                found = list(fn() or [])
            except Exception as e:  # noqa: BLE001 — one bad scanner must not sink the cycle
                logger.warning("scanner %s failed (non-fatal): %s", name, e)
                found = []
            triggers.extend(found)
            self.scheduler.record_yield(name, len(found))
        return triggers

    # -- one cycle ---------------------------------------------------------

    def run_once(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Run exactly one cycle. Returns a small summary dict.

        `now` overrides "the current time" for the quiet-hours check only
        — real callers should omit it; tests use it to exercise a fixed
        clock deterministically.
        """
        triggers = self._collect_triggers()
        route_triggers_to_modules(triggers, self.modules)

        content = self.workspace.run_cycle({"trigger_count": len(triggers)})
        winner_dict = content.to_dict() if content else None

        state = self._transition_state(winner_dict)
        save_state(state)

        journal.append_event("cycle", {
            "trigger_count": len(triggers),
            "bids": self.workspace.get_stats()["last_bids"],
            "winner": winner_dict,
            "state": state.get("state"),
        })

        result: Dict[str, Any] = {
            "trigger_count": len(triggers),
            "winner": winner_dict,
            "state": state.get("state"),
            "harness_results": [],
        }

        if content is not None:
            result["harness_results"] = self._dispatch_to_harnesses(content, winner_dict, state, now)

        return result

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
        if isinstance(urgency, (int, float)) and urgency >= ALERT_URGENCY_THRESHOLD:
            state = transition_state(
                state, "ALERT", f"winner urgency {urgency:.2f} >= {ALERT_URGENCY_THRESHOLD}"
            )
            journal.append_event("alarm", {
                "detail": f"{winner_dict['source_module']}: {str(winner_dict['content'])[:200]}",
                "urgency": urgency,
            })
            return state
        return transition_state(state, "FOCUSED", f"winner from {winner_dict['source_module']}")

    # -- winner -> plan -> briefing -> gate -> harness --------------------

    @staticmethod
    def _harness_wants(hc: Dict[str, Any], winner_dict: Dict[str, Any]) -> bool:
        """Apply a harness config's optional wake filter to this winner."""
        modules = hc.get("wake_modules")
        if isinstance(modules, list) and modules:
            if winner_dict.get("source_module") not in modules:
                return False
        floor = hc.get("wake_min_salience")
        if isinstance(floor, (int, float)):
            salience = winner_dict.get("salience", 0.0)
            if not isinstance(salience, (int, float)) or salience < floor:
                return False
        return True

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
            enabled_configs = [
                hc for hc in enabled_configs if self._harness_wants(hc, winner_dict)
            ]
            if not enabled_configs:
                journal.append_event("wake_filtered", {
                    "winner_module": winner_dict.get("source_module"),
                    "salience": winner_dict.get("salience"),
                })
                return []

            quiet = command_actuator.load_quiet_hours()
            if is_quiet_hours(quiet, now):
                for hc in enabled_configs:
                    journal.append_event("quiet_hours_deferred", {
                        "harness": hc["name"], "winner": winner_dict,
                    })
                return []

            budget = {"remaining": 10.0}  # the planner's own queue-depth gate, not the $ budget
            plan = self.planner.propose_plan(content, budget)

            since_iso = state.get("last_wake")
            briefing = journal.build_briefing(since_iso, winner=winner_dict)

            intent = {
                "kind": "decide",
                "summary": f"ZugaMind workspace winner: {str(winner_dict['content'])[:200]}",
                "context": {"winner": winner_dict, "plan": plan},
                "caller": "stream.runner",
            }
            gate_result = escalate_for_action(intent, dry_run=self.dry_run)

            if not gate_result.get("ok"):
                journal.append_event("harness_skip", {
                    "reason": gate_result.get("reason", "gate_not_ok"),
                })
                return []

            harness_results = [
                command_actuator.invoke_harness(hc, briefing, dry_run=self.dry_run)
                for hc in enabled_configs
            ]

            if harness_results:
                state["last_wake"] = journal.now_iso()
                save_state(state)

            return harness_results
        except Exception as e:  # noqa: BLE001 — fail-closed: no gate error reaches a harness call
            logger.warning("harness dispatch failed (fail-closed, no harness invoked): %s", e)
            journal.append_event("harness_skip", {"reason": f"runner_error:{e}"})
            return []

    # -- multi-cycle / daemon ----------------------------------------------

    def run_cycles(self, n: int) -> List[Dict[str, Any]]:
        return [self.run_once() for _ in range(n)]

    def run_daemon(self, interval: int = DEFAULT_DAEMON_INTERVAL_SEC) -> None:
        """Loop `run_once()` forever until SIGINT/SIGTERM, then save state,
        journal a "shutdown" event, and return."""
        stop = {"flag": False}

        def _handle_signal(signum, frame):  # noqa: ANN001 — stdlib signal handler signature
            logger.info("stream.runner received signal %s — shutting down after this cycle", signum)
            stop["flag"] = True

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        logger.info("stream.runner daemon starting (interval=%ss, dry_run=%s)", interval, self.dry_run)
        while not stop["flag"]:
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001 — one bad cycle must not kill the daemon
                logger.warning("cycle failed (non-fatal, continuing): %s", e)
            for _ in range(max(1, interval)):
                if stop["flag"]:
                    break
                time.sleep(1)

        state = load_state()
        save_state(state)
        journal.append_event("shutdown", {"reason": "signal"})
        logger.info("stream.runner daemon shutdown complete")


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
    args = _build_arg_parser().parse_args(argv)
    runner = StreamRunner(dry_run=args.dry_run)

    if args.daemon:
        runner.run_daemon(interval=args.interval)
        return 0

    n = args.cycles if args.cycles else 1
    results = runner.run_cycles(n)
    for i, r in enumerate(results, 1):
        winner = r["winner"]
        summary = (f"{winner['source_module']}: {str(winner['content'])[:80]}"
                   if winner else "(no winner)")
        print(f"cycle {i}/{n} state={r['state']} triggers={r['trigger_count']} winner={summary}")
        for hr in r["harness_results"]:
            print(f"  harness[{hr.get('harness')}] ok={hr.get('ok')} dry_run={hr.get('dry_run')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
