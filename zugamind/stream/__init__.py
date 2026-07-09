"""ZugaMind stream package — the always-on cognition loop.

Ties scanners (perception) -> the GWT workspace (attention) -> the
fail-closed action gate -> act.command_actuator (harness wake) into a
single runnable loop. See runner.py for the CLI:

    python -m stream.runner --once
"""

from .runner import StreamRunner

__all__ = ["StreamRunner"]
