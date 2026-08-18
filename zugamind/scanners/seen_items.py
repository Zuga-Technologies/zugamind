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
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

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
        return {str(k): float(v) for k, v in data.items()}
    except Exception as e:
        logger.debug("seen-set load failed (%s): %s", path.name, e)
        return None


def write_seen(path: Path, seen: dict[str, float], max_keys: int) -> None:
    """Persist the newest `max_keys` entries.

    Pruning is by recency and never by key order: an alphabetical cap (the
    shape the example scanners used) can evict an item that is still on the
    feed, which lets it fire again on the next sweep — the exact bug the
    seen-set exists to prevent.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if len(seen) > max_keys:
            seen = dict(sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:max_keys])
        path.write_text(json.dumps(seen), encoding="utf-8")
    except Exception as e:
        logger.debug("seen-set save failed (%s): %s", path.name, e)
