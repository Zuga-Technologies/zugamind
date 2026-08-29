"""Per-scanner "already emitted" memory, for sources whose top is sticky.

Habituation (see this package's `habituation_filter`) is a FORGETTING window:
a trigger is damped for HABITUATION_HOURS and then allowed through again,
which is right for a condition that can recur — a service is down again, a
worktree is dirty again. It is wrong for a feed ITEM. A published post does
not become news a second time, so on a source that keeps something at the top
of its feed on purpose, a forgetting window is just a rate limiter: the item
re-fires every window, forever.

Measured on this deployment's journal, 2026-08-18, over 576 world_signals
workspace wins:

     n  source            what it was
    24  anthropic.com     a pinned/featured card
    12  r/singularity     "Discord Server Link" (stickied)
     9  anthropic.com     "Introducing Claude Opus 5", published Jul 24
     9  r/MachineLearning "[D] Self-Promotion Thread" (stickied)
     8  r/LocalLLaMA      "Best Local LLMs - August 2026" (stickied)

148 of those 576 wins — 26% — were repeats. Every top offender is content a
platform deliberately pins. So a scanner over a feed needs to remember what
it has already said, permanently, and that is what this module is.

Kept deliberately tiny and path-explicit so each scanner keeps owning its own
file and stays isolable in tests by patching its own cache directory.

Every write here goes through the engine's ONE atomic file writer,
`foundation.fs.atomic_write_text` (re-exported from this module, where it
lived first), so "a killed process cannot leave a torn JSON file behind" is
a property of one function, not a pattern each file has to get right on its
own. (The `examples/` scanners carry their own copy on purpose: they are
documented as copy-and-run with zero imports from the engine.)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from foundation.fs import atomic_write_text  # noqa: F401  (re-exported; lived here first)

logger = logging.getLogger("zugamind.scanners.seen_items")


def read_seen(path: Path) -> dict[str, float] | None:
    """Item key -> epoch first emitted.

    Returns None for "no usable seen-set on disk", which callers MUST treat
    as a cold start distinct from an empty one: an empty dict means we really
    have seen nothing, while None means we have no idea and must baseline
    rather than read a whole back catalogue as breaking news.
    """
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        # Coerce per entry, not in one comprehension. A single unparseable
        # value used to raise and discard the ENTIRE seen-set (500+ remembered
        # items thrown away by one null), which reads to the caller as a cold
        # start and re-baselines the whole feed.
        out: dict[str, float] = {}
        for k, v in data.items():
            try:
                ts = float(v)
            except (TypeError, ValueError):
                continue
            if ts == ts and ts not in (float("inf"), float("-inf")):  # drop NaN/inf
                out[str(k)] = ts
        return out
    except Exception as e:
        logger.debug("seen-set load failed (%s): %s", path.name, e)
        return None


def write_seen(path: Path, seen: dict[str, float], max_keys: int,
               protect: "set[str] | None" = None) -> None:
    """Persist the newest `max_keys` entries.

    Pruning is by recency and never by key order: an alphabetical cap (the
    shape the example scanners used) can evict an item that is still on the
    feed, which lets it fire again on the next sweep — the exact bug the
    seen-set exists to prevent.

    `protect`: keys STILL VISIBLE on the feed this sweep, which are exempt
    from eviction. Recency alone is not enough, because the stored timestamp
    is when an item was FIRST seen and is never refreshed. A pinned or
    stickied post therefore holds the oldest timestamp in the set no matter
    how long it stays up, so it is the FIRST thing evicted when the cap trips
    — and then re-fires as if brand new. That is precisely the bug this
    module was written to prevent, and it was invisible to the suite because
    the existing test only proves recency beats alphabetical order (audit
    2026-08-29; measured then as ~32 days from tripping on the live
    ai_labs set).
    """
    try:
        if max_keys <= 0:
            # Writing {} here silently wipes the set and re-fires everything.
            logger.warning("seen-set %s: max_keys=%d is not a cap, refusing to "
                           "wipe the set", path.name, max_keys)
            return
        if len(seen) > max_keys:
            keep = dict(sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:max_keys])
            for key in (protect or ()):
                if key in seen:
                    keep[key] = seen[key]
            seen = keep
        atomic_write_text(path, json.dumps(seen))
    except Exception as e:
        # Not .debug: an unwritable seen-set means the caller re-baselines
        # every cycle and the source goes permanently dark with no line an
        # operator can find at default level.
        logger.warning("seen-set save failed (%s): %s — this source will "
                       "re-baseline every cycle until it succeeds", path.name, e)
