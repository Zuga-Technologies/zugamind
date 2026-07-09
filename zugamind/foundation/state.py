"""Cognitive state machine — load/save/transition + temporal decay.

A simple internal state machine layered under the GWT workspace. The six
cognitive states (RESTING / CURIOUS / FOCUSED / ALERT / REFLECTING /
DREAMING) are the canonical list in `STATES`. State transitions are logged
via the standard `logging` module — no external event-stream dependency.
"""

import json
import logging
from datetime import datetime

from foundation.config import ENGINE_DIR, STATE_FILE

logger = logging.getLogger("zugamind.state")

# Cognitive states
STATES = ["RESTING", "CURIOUS", "FOCUSED", "ALERT", "REFLECTING", "DREAMING"]


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


# --- Temporal state decay ----------------------------------------------------


def apply_temporal_decay(state: dict) -> dict:
    """Decay cognitive state based on time elapsed since last transition.

    Prevents stuck states by forcing transitions when idle too long.
    Night/morning rules override all other decay logic.
    """
    now = datetime.now()
    hour = now.hour
    since = datetime.fromisoformat(state.get("since", now.isoformat()))
    idle_minutes = (now - since).total_seconds() / 60

    # Night mode: any state -> DREAMING between 11 PM and 6 AM
    if hour >= 23 or hour < 6:
        if state["state"] != "DREAMING":
            return transition_state(state, "DREAMING", f"night mode (hour={hour})")
    # Wake up: DREAMING -> RESTING between 6-8 AM
    elif state["state"] == "DREAMING" and 6 <= hour < 8:
        return transition_state(state, "RESTING", "morning wake-up")
    # ALERT >15 min idle -> CURIOUS
    elif state["state"] == "ALERT" and idle_minutes > 15:
        return transition_state(state, "CURIOUS", f"ALERT idle {idle_minutes:.0f}min")
    # FOCUSED >60 min idle -> CURIOUS
    elif state["state"] == "FOCUSED" and idle_minutes > 60:
        return transition_state(state, "CURIOUS", f"FOCUSED idle {idle_minutes:.0f}min")
    # CURIOUS >30 min idle -> RESTING
    elif state["state"] == "CURIOUS" and idle_minutes > 30:
        return transition_state(state, "RESTING", f"CURIOUS idle {idle_minutes:.0f}min")
    # RESTING >6 hours -> DREAMING
    elif state["state"] == "RESTING" and idle_minutes > 360:
        return transition_state(state, "DREAMING", f"RESTING idle {idle_minutes:.0f}min")

    return state
