"""Filesystem primitives the whole engine shares.

`atomic_write_text` is the engine's ONE atomic file writer. Every piece of
persistent state — cognitive state, budget, seen-sets, fetch caches, the
calibration file — goes through it, so "a killed process cannot leave a torn
JSON file behind" is a property of one function rather than a pattern each
module has to get right on its own. It lived in scanners/seen_items.py for
one morning (2026-08-28) before the act/ audit found the same class in
act/floor_calibration.py, foundation/state.py and six other sites — a base
layer utility belongs in the base layer. seen_items still re-exports it.

Stdlib-only.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` so a reader can never observe a torn file.

    Same-directory temp file + `os.replace`, which is atomic on POSIX and on
    Windows when source and destination share a filesystem (they do: same
    directory). A crash between the write and the replace leaves the OLD file
    untouched — stale beats corrupt, because a corrupt file reads as "no
    state" and "no state" means a cold start, a re-baselined feed, a floor
    reset to warm-up, a budget reset to zero.

    The temp name is unique per call (`mkstemp`), NOT a fixed `<name>.tmp`.
    Two processes really do write the same files on a deployment (a
    long-running daemon and a hand-run CLI sweep): with a shared fixed name,
    writer B truncates writer A's temp mid-write and one of the two
    `replace` calls fails on a file that no longer exists — a lost write that
    only ever shows up as a debug log line. With unique temps both writes
    land and the last replace wins.

    Raises on failure so callers keep deciding whether persistence is
    best-effort; the temp file is removed on any failure so a crash cannot
    litter the directory.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        # The handle is closed by now — Windows refuses to replace an open file.
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


__all__ = ["atomic_write_text"]
