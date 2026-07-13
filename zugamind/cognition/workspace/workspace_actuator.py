"""
ZugaMind Workspace Actuator — feedback loop for workspace health.

**Reference-only (issue #5): complete and tested, but not instantiated by
StreamRunner, the demo, or wired into `cognition.workspace`'s public
`__all__`.** The attention schema already does per-cycle streak damping and
diversity capping; this is the slower, integral-term counterpart — fairness
over tens of cycles rather than within one. To wire it in: instantiate in
`StreamRunner.__init__` and call `on_cycle_complete()` from `run_once()`.

Runs every N cycles (default 10). Reads workspace stats and writes
dampening/boost adjustments directly to the AttentionSchema. Adjustments
decay a small amount every cycle, so the actuator must keep re-evaluating
to maintain corrections — a one-off nudge fades rather than becoming a
permanent thumb on the scale.

Logs Critical Process Parameters (CPPs) to <data_dir>/workspace_cpps.jsonl.
State persisted to <data_dir>/actuator_state.json.

Zero pip dependencies (stdlib only).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("zugamind.workspace_actuator")

try:
    from foundation.config import ENGINE_DIR
except Exception:  # pragma: no cover - allows standalone use without foundation/
    ENGINE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "engine"

CPPS_FILE = ENGINE_DIR / "workspace_cpps.jsonl"
ACTUATOR_STATE_FILE = ENGINE_DIR / "actuator_state.json"

ACTUATOR_INTERVAL = 10  # run a full check every N cycles

MAX_BOOST = 0.15
MAX_PENALTY = -0.15

# Optional extension point: a callable returning a risk assessment dict with
# at least {"p_failure": float in [0,1]}, or None if unavailable. Wire your
# own risk model here (e.g. a canary-deploy failure predictor); left as None
# by default so this module has zero external dependencies out of the box.
DesignSpaceCheck = Callable[[], Optional[Dict[str, Any]]]


class WorkspaceActuator:
    """Feedback actuator for the workspace's attention health.

    Three correction mechanisms:
    1. Module starvation : boost modules that haven't won in many cycles
    2. Domination         : penalize modules winning >40% of recent cycles
    3. Risk check (opt-in): if an injected risk model reports elevated
                            failure probability, suppress action-heavy modules
    """

    def __init__(self, design_space_check: Optional[DesignSpaceCheck] = None):
        self._cycles_since_check: int = 0
        self._last_check_cycle: int = 0
        self._total_checks: int = 0
        self._design_space_check = design_space_check
        self._load_state()

    def _load_state(self):
        if ACTUATOR_STATE_FILE.exists():
            try:
                data = json.loads(ACTUATOR_STATE_FILE.read_text())
                self._cycles_since_check = data.get("cycles_since_check", 0)
                self._last_check_cycle = data.get("last_check_cycle", 0)
                self._total_checks = data.get("total_checks", 0)
            except (json.JSONDecodeError, OSError):
                pass

    def _save_state(self):
        ENGINE_DIR.mkdir(parents=True, exist_ok=True)
        state = {
            "cycles_since_check": self._cycles_since_check,
            "last_check_cycle": self._last_check_cycle,
            "total_checks": self._total_checks,
            "last_updated": datetime.now().isoformat(),
        }
        ACTUATOR_STATE_FILE.write_text(json.dumps(state, indent=2))

    def on_cycle_complete(self, workspace_stats: Dict[str, Any],
                          attention_schema, cycle_number: int) -> Dict[str, Any]:
        """Call after each workspace cycle. Runs a full check every
        ACTUATOR_INTERVAL cycles; returns {} otherwise."""
        self._cycles_since_check += 1
        if self._cycles_since_check < ACTUATOR_INTERVAL:
            return {}

        self._cycles_since_check = 0
        self._last_check_cycle = cycle_number
        self._total_checks += 1

        result = self._run_check(workspace_stats, attention_schema)
        self._log_cpps(workspace_stats, result, cycle_number)
        self._save_state()
        return result

    def _run_check(self, stats: Dict[str, Any], attention_schema) -> Dict[str, Any]:
        adjustments: Dict[str, float] = {}
        schema_ctx = stats.get("attention_schema", {})
        win_counts = attention_schema.module_win_counts
        total_cycles = attention_schema._total_cycles

        if total_cycles < 10:
            return {"status": "warmup", "adjustments": {}}

        # 1. Starvation — iterate REGISTERED modules (not just win_counts keys)
        # so a module that has NEVER won (zero entries in win_counts) is still
        # visible to the boost.
        registered = stats.get("registered_modules") or list(win_counts.keys())
        for module in registered:
            if module == "metacognition":
                continue  # mirrors blind_spots' deliberate exclusion
            wins = win_counts.get(module, 0)
            win_rate = wins / total_cycles if total_cycles > 0 else 0
            never_won = wins == 0
            if win_rate < 0.05 and (never_won or module in (schema_ctx.get("blind_spots") or [])):
                adjustments[module] = adjustments.get(module, 0) + 0.08
                logger.info("[Actuator] Boosting starved module %s (+0.08%s)",
                            module, ", never won" if never_won else "")

        # 2. Domination (> 40% of recent wins)
        recent_modules = [f["module"] for f in attention_schema.recent_foci[-10:]]
        if len(recent_modules) >= 5:
            for module in set(recent_modules):
                recent_rate = recent_modules.count(module) / len(recent_modules)
                if recent_rate > 0.4:
                    penalty = max(MAX_PENALTY, -0.05 * (recent_rate - 0.4) / 0.1)
                    adjustments[module] = adjustments.get(module, 0) + penalty
                    logger.info("[Actuator] Penalizing dominant module %s (%.3f, rate=%.0f%%)",
                                module, penalty, recent_rate * 100)

        # 3. Optional risk check
        design_space_result = self._check_design_space()
        if design_space_result and design_space_result.get("p_failure", 0) > 0.4:
            p_fail = design_space_result["p_failure"]
            for module in ("daemon", "code_changes"):
                penalty = -0.05 * min(1.0, (p_fail - 0.4) / 0.4)
                adjustments[module] = adjustments.get(module, 0) + penalty
            logger.info("[Actuator] Risk check elevated (P(fail)=%.2f) — suppressing action modules",
                        p_fail)

        for module, adj in adjustments.items():
            adj = max(MAX_PENALTY, min(MAX_BOOST, adj))
            attention_schema.set_adjustment(module, adj)

        return {
            "status": "checked",
            "adjustments": adjustments,
            "design_space": design_space_result,
            "total_checks": self._total_checks,
        }

    def _check_design_space(self) -> Optional[Dict[str, Any]]:
        if self._design_space_check is None:
            return None
        try:
            return self._design_space_check()
        except Exception as e:
            logger.debug("[Actuator] design space check failed: %s", e)
            return None

    def _log_cpps(self, stats: Dict[str, Any], check_result: Dict[str, Any], cycle_number: int):
        ENGINE_DIR.mkdir(parents=True, exist_ok=True)
        cpp = {
            "timestamp": datetime.now().isoformat(),
            "cycle": cycle_number,
            "workspace_cycle_count": stats.get("cycle_count", 0),
            "module_count": stats.get("module_count", 0),
            "attention": stats.get("attention_schema", {}),
            "adjustments": check_result.get("adjustments", {}),
            "design_space_p_failure": (
                check_result.get("design_space", {}).get("p_failure")
                if check_result.get("design_space") else None
            ),
            "current_winner": (
                stats.get("current_content", {}).get("source_module")
                if stats.get("current_content") else None
            ),
        }
        try:
            with open(CPPS_FILE, "a") as f:
                f.write(json.dumps(cpp) + "\n")
        except Exception as e:
            logger.debug("[Actuator] Failed to write CPPs: %s", e)
