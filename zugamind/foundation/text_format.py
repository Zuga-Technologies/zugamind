"""Shared text-truncation helper for anything a human reads.

Found scattered across the codebase as ad-hoc `text[:N]` slices with no
word-boundary handling -- caught live when a scanner title landed cut off
mid-word. This is the single copy; anything truncating title/summary/detail/
reason text for display should import this instead of slicing directly.

Stdlib only.
"""
from __future__ import annotations


def truncate_title(text: str, limit: int = 70) -> str:
    """Shorten text for display without cutting mid-word.

    Cuts at the last space before `limit` and appends an ellipsis; falls
    back to the hard limit only when there's no space to cut at (a single
    token longer than `limit`)."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = head.rsplit(" ", 1)[0]
    return (cut if cut else head) + "…"


# Payload compaction (2026-08-28). A workspace winner carries its triggers
# verbatim, a plan step copies the winner's whole context, and both were
# json-dumped uncapped into (a) the paid model's prompt and (b) every
# per-cycle journal event. Measured: five triggers with 5 KB details became a
# 51,939-char prompt (the plan re-embedded the winner's own triggers) and a
# 25 KB journal line per cycle. This clips strings, caps lists and bounds
# nesting so what reaches a model or a log is a summary of the payload, not
# the payload — the in-memory objects are never touched.
COMPACT_MAX_STR = 300
COMPACT_MAX_ITEMS = 8
COMPACT_MAX_DEPTH = 6


def compact_payload(obj, max_str: int = COMPACT_MAX_STR, max_items: int = COMPACT_MAX_ITEMS,
                    max_depth: int = COMPACT_MAX_DEPTH, _depth: int = 0):
    """A bounded COPY of a JSON-ish payload: long strings clipped with an
    ellipsis, lists/dicts capped at `max_items` (with a marker noting how
    many were dropped), nesting cut at `max_depth`. Never raises."""
    try:
        if isinstance(obj, str):
            return obj if len(obj) <= max_str else obj[: max_str - 1] + "…"
        if isinstance(obj, (int, float, bool)) or obj is None:
            return obj
        if _depth >= max_depth:
            return "…"
        if isinstance(obj, dict):
            out = {}
            for i, (k, v) in enumerate(obj.items()):
                if i >= max_items:
                    out["…"] = f"+{len(obj) - max_items} more keys"
                    break
                out[str(k)] = compact_payload(v, max_str, max_items, max_depth, _depth + 1)
            return out
        if isinstance(obj, (list, tuple, set)):
            seq = list(obj)
            out = [compact_payload(v, max_str, max_items, max_depth, _depth + 1) for v in seq[:max_items]]
            if len(seq) > max_items:
                out.append(f"…+{len(seq) - max_items} more")
            return out
        return compact_payload(str(obj), max_str, max_items, max_depth, _depth)
    except Exception:  # noqa: BLE001 — compaction is for logs and prompts; never break the caller
        return "…"
