"""ZugaMind act package — the harness adapter.

Turns an approved workspace decision into a real subprocess invocation of
the user's agent harness (Claude Code, OpenClaw, Hermes, a generic webhook,
...). See command_actuator.py for the fail-closed contract: callers MUST
pass every invocation through gates.action_gate first — this package only
executes, it never decides.
"""

from .command_actuator import (
    DEFAULT_HARNESS_CONFIG,
    invoke_harness,
    load_harness_configs,
    load_quiet_hours,
)

__all__ = ["invoke_harness", "load_harness_configs", "load_quiet_hours", "DEFAULT_HARNESS_CONFIG"]
