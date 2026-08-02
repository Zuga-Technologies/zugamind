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
