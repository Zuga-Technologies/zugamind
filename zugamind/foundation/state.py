"""Cognitive state machine — load/save/transition.

A simple internal state machine layered under the GWT workspace. The four
cognitive states (RESTING / FOCUSED / ALERT / REFLECTING) are the canonical
list in `STATES` — the states `StreamRunner._transition_state` actually
produces. State transitions are logged via the standard `logging` module —
no external event-stream dependency.
"""

import json
import logging
from datetime import datetime

from foundation.config import ENGINE_DIR, STATE_FILE
from foundation.fs import atomic_write_text

logger = logging.getLogger("zugamind.state")

# Cognitive states
STATES = ["RESTING", "FOCUSED", "ALERT", "REFLECTING"]


def _fresh() -> dict:
    return {
        "state": "RESTING",
        "since": datetime.now().isoformat(),
        "last_cycle": None,
        "cycles_today": 0,
        "last_transition": None,
        "focus_topic": None,
    }


def load_state() -> dict:
    """Load current cognitive state. Never raises.

    Fails OPEN, and that is the whole point. This used to let a JSONDecodeError
    out of json.loads, and the one call site that could have repaired the file
    -- StreamRunner._transition_state -> _save_state_safe -- sits OUTSIDE any
    try/except in run_once. So a truncated state.json (a hard kill mid-write,
    a full disk) made every cycle raise before reaching the code that would
    have overwritten the poison: the daemon stayed up, looked alive, and did
    zero cognition, permanently (audit 2026-08-29).

    act/floor_calibration.py already learned this exact lesson and says so in
    its own docstring. This is that rule applied here: a corrupt file reads as
    "no state", "no state" means a cold start, and the next save rewrites it.
    """
    try:
        if STATE_FILE.exists():
            loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded
            logger.warning("state file is %s, not an object — starting fresh",
                           type(loaded).__name__)
        else:
            return _fresh()
    except Exception as e:  # noqa: BLE001 — a bad state file is not a dead daemon
        logger.warning("state file unreadable (%s) — starting fresh; the next "
                       "save will overwrite it", e)
    return _fresh()


def save_state(state: dict) -> None:
    """Persist cognitive state."""
    ENGINE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(STATE_FILE, json.dumps(state, indent=2))


def transition_state(current: dict, new_state: str, reason: str) -> dict:
    """Transition to a new cognitive state with logging."""
    old = current["state"]
    if old != new_state:
        current["state"] = new_state
        current["since"] = datetime.now().isoformat()
        current["last_transition"] = {
            "from": old,
            "to": new_state,
            "reason": reason,
            "at": datetime.now().isoformat(),
        }
        logger.info("State: %s -> %s (%s)", old, new_state, reason)
    return current
