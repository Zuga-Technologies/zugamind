"""ZugaMind continuity package — episodic journal + harness wake briefings.

Scanners are perception, the workspace is attention; continuity is memory —
the record of what happened between one harness wake and the next, and the
briefing generator that turns that record into something a stateless
harness invocation can read on its way in. Re-exports the public surface so
callers can `from continuity import journal` or import names directly.
"""

from .journal import append_event, build_briefing, now_iso, read_events, JOURNAL_FILE

__all__ = ["append_event", "read_events", "build_briefing", "now_iso", "JOURNAL_FILE"]
