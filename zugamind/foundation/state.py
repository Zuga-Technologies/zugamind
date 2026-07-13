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

logger = logging.getLogger("zugamind.state")

# Cognitive states
STATES = ["RESTING", "FOCUSED", "ALERT", "REFLECTING"]


def load_state() -> dict:
    """Load current cognitive state."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "state": "RESTING",
        "since": datetime.now().isoformat(),
        "last_cycle": None,
        "cycles_today": 0,
        "last_transition": None,
        "focus_topic": None,
    }


def save_state(state: dict) -> None:
    """Persist cognitive state."""
    ENGINE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


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
