"""Outbound-thought path: the socket share_filter was written for.

Until 2026-08-29 `gates.share_filter.should_share` had no caller. There was
no path a candidate thought travelled down, so there was nothing for it to
guard -- working code that nothing calls reads exactly like protection. This
is the path, kept deliberately small: a caller hands over a candidate thought
in the exact shape the guard already reads
({text, confidence, topic_class, proposed_action, ask_text}), the guard
decides, and the verdict is journaled either way.

Where a shared thought GOES (decision 1, Buga, 2026-08-29): the journal only.
A `thought_shared` event is a record a human reads later. No channel
(Discord, webhook) exists in this core and adding one is a product decision,
not a wiring detail; it can be bolted on downstream of this function without
touching the guard, because the guard decides worth-sharing, never delivery.

Ships DARK: ZUGAMIND_THOUGHTS_ENABLED defaults false. Off, the guard still
runs and its verdict is still journaled -- as `thought_suppressed` carrying
the guard's own reason and enabled=false -- so "would have shared" is
verifiable from the journal before anything is switched on. value_gate's
pattern.

FAIL-OPEN, like the guard it wraps: a guard that crashes must not silence the
agent, so an exception inside should_share counts as share, with a reason
that says so. Never raises into the caller. Stdlib only.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from continuity import journal
from gates.share_filter import should_share

logger = logging.getLogger("zugamind.thoughts")

# The fields the guard reads; the journal record carries exactly these plus
# the verdict, so a reader can re-run the guard on the record by hand.
_FIELDS = ("text", "confidence", "topic_class", "proposed_action", "ask_text")


def _enabled() -> bool:
    return os.environ.get(
        "ZUGAMIND_THOUGHTS_ENABLED", "false",
    ).strip().lower() not in ("0", "false", "no", "off", "")


def consider_thought(thought: dict[str, Any]) -> dict[str, Any]:
    """Run one candidate thought through share_filter and journal the verdict.

    Returns {"shared": bool, "reason": str, "enabled": bool}. Exactly one
    journal event is written: `thought_shared` when the flag is on AND the
    guard says share; otherwise `thought_suppressed`, always with a reason
    (invariant 5: a silent drop is indistinguishable from a broken gate).
    """
    if not isinstance(thought, dict):
        thought = {"text": str(thought)}
    enabled = _enabled()
    try:
        share, reason = should_share(thought)
    except Exception as exc:  # noqa: BLE001 — fail-open by contract (invariant 3)
        share, reason = True, f"guard_error:{type(exc).__name__}:{str(exc)[:120]}"
        logger.warning("thoughts: share_filter raised, failing open: %s", exc)

    shared = bool(share and enabled)
    record = {k: thought.get(k) for k in _FIELDS}
    record.update({"verdict": bool(share), "reason": reason, "enabled": enabled})
    journal.append_event("thought_shared" if shared else "thought_suppressed", record)
    return {"shared": shared, "reason": reason, "enabled": enabled}
