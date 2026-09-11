"""A source that has stopped answering should reach the mind, not just the log.

Until 2026-09-11 a dead feed produced exactly one WARNING per cycle and
nothing else. On the BugaPC twin that let `github_issues:Zuga-Technologies/Ludus`
reach 1,816 consecutive failures and `repo_events:Zuga-Technologies/Ludus`
reach 636, both unnoticed for weeks, while the agent went on reasoning as
though it had full sight of that repo. (The cause was a missing GitHub
credential: the repo is private, and GitHub answers 404 rather than 403 for a
private repo you cannot see, so "no token" and "no repo" are the same answer.
A scanner cannot tell them apart, which is exactly why a human has to be told.)

This scanner turns `safe_http.dead_sources()` into `system_health` triggers.

WHY IT GOES QUIET, WHICH IS THE WHOLE DESIGN
--------------------------------------------
A permanently dead source is permanently true, and a permanently true trigger
is how you build a system that wakes a paid session every cycle forever. That
failure has already happened once in this codebase, in a different shape (the
priority-goals clock, fixed the same night), so the damping here is deliberate
and layered:

  1. The trigger's identity carries an OCTAVE, not the exact failure count:
     the streak threshold, then each doubling of it (3, 6, 12, 24, 48, ...).
     The id changes only when the streak doubles, so a source dead for a year
     mints about fifteen distinct ids instead of one per cycle.
  2. Habituation (scanners.habituation_filter, 6 h by default) damps each of
     those ids, so even a fresh octave fires at most four times a day.
  3. `detail` carries the same octave, never the exact count. `_trigger_key`
     falls back to a hash of `detail` when no id field is present, so a detail
     that changes every cycle would defeat layer 2 outright if anyone ever
     removed the id. Octave-in-both keeps the two consistent by construction.
  4. `urgency` stays well under 0.9. At 0.9 a trigger enters the alarm lane,
     which bypasses the wake floor entirely.

WHAT IT IS WORTH, DELIBERATELY
------------------------------
`system_health` lands in InfrastructureModule's DEGRADED branch, whose bid is
`min(0.7, 0.4 + 0.1 * n)` and ignores novelty/relevance/urgency entirely: only
the COUNT of triggers moves it. So

    1 dead source  -> 0.50   under a typical learned floor: logged, visible, no paid wake
    2 dead sources -> 0.60   over it: worth a session
    3+             -> 0.70

That is the intended calibration. One broken feed is a log line a human can
find. Two at once is a pattern (a revoked credential, a dead network path, an
API that changed), and that is worth waking for.

Stdlib only, no network: reads an in-process registry and sorts it.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from .. import safe_http

# At most this many triggers per cycle. The degraded bid saturates at three
# anyway (0.4 + 0.1*3 = 0.7, the branch cap), so more would add salience but
# no information, and would crowd the cycle's trigger list.
MAX_TRIGGERS = 3


def _octave(fails: int, threshold: int) -> int:
    """The largest doubling of `threshold` that is <= `fails`.

    threshold=3 -> 3,4,5 all read 3; 6..11 read 6; 12..23 read 12; and so on.
    This is the trigger's identity, so it changes on a logarithmic schedule
    rather than every cycle.
    """
    step = max(1, int(threshold))
    fails = int(fails)
    while step * 2 <= fails:
        step *= 2
    return step


def scan_dead_sources(now: float | None = None) -> List[Dict[str, Any]]:
    """`system_health` triggers for sources past the loud-failure threshold."""
    if now is None:
        now = time.time()
    try:
        dead = safe_http.dead_sources()
    except Exception:  # noqa: BLE001 — a health scanner must never break the cycle
        return []
    if not dead:
        return []

    threshold = getattr(safe_http, "_FAILS_BEFORE_LOUD", 3)
    # Worst first, so the cap keeps the most-dead rather than the
    # alphabetically-luckiest. The label breaks ties so the order is stable.
    ranked = sorted(dead.items(), key=lambda kv: (-int(kv[1].get("fails") or 0), kv[0]))

    triggers: List[Dict[str, Any]] = []
    for label, info in ranked[:MAX_TRIGGERS]:
        fails = int(info.get("fails") or 0)
        octave = _octave(fails, threshold)
        first_seen = float(info.get("first_seen") or now)
        hours = max(0.0, (now - first_seen) / 3600.0)
        reason = str(info.get("detail") or "no detail")[:120]
        triggers.append({
            "type": "system_health",
            # Octave, never `fails`: see layer 3 in the module docstring.
            "detail": (f"{label} has returned nothing for {octave}+ consecutive "
                       f"attempts ({reason}) — this source is dark, and the "
                       f"agent has been reasoning without it"),
            "id": f"dead_source:{label}:{octave}",
            "novelty": 0.5,
            "relevance": 0.9,
            # Under 0.9 on purpose: 0.9 opens the alarm lane, which bypasses
            # the wake floor. A dark feed is degraded, not an emergency.
            "urgency": 0.4,
            "service": label,
            "source_label": label,
            # The exact count and age ride along for a reader; neither is in
            # _ID_FIELDS and neither is in `detail`, so neither can reach the
            # habituation key.
            "fails": fails,
            "dead_for_hours": round(hours, 1),
        })
    return triggers
