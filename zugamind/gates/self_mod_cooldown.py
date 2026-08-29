"""Persistent per-file cooldown for cognition self-modifications.

An in-memory-only cooldown is lost on restart, so the agent could churn the
same control file by bouncing the process. This is the on-disk equivalent:
once a cognition mod is proposed for a file, that file is on cooldown for
`COOLDOWN_HOURS`. A fresh process re-reads the same sqlite file, so the
cooldown SURVIVES a restart.

Stdlib + sqlite3 only.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

COOLDOWN_HOURS = 24.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_cooldown (
    path    TEXT PRIMARY KEY,
    last_ts REAL NOT NULL
);
"""


def _default_db_path() -> Path:
    """Sibling of the cognition-mod audit log so the two share a data dir."""
    try:
        from foundation.config import DATA_DIR
        default_audit = str(DATA_DIR / "cognition_mod_audit.jsonl")
    except Exception:
        default_audit = str(Path(os.getcwd()) / "data" / "cognition_mod_audit.jsonl")
    audit = os.environ.get("ZUGAMIND_COGNITION_MOD_AUDIT", default_audit)
    return Path(audit).parent / "cognition_mod_cooldown.db"


class SelfModCooldown:
    """Disk-backed per-file cooldown. Restart-durable (unlike an in-memory one)."""

    def __init__(self, db_path: Optional[Path] = None,
                 cooldown_hours: float = COOLDOWN_HOURS):
        self.db_path = Path(db_path) if db_path is not None else _default_db_path()
        self.cooldown_seconds = cooldown_hours * 3600.0
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def record(self, file_path: str, *, now: Optional[float] = None) -> None:
        """Stamp `file_path` as just-modified, starting its cooldown window."""
        ts = time.time() if now is None else now
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO file_cooldown (path, last_ts) VALUES (?, ?) "
                "ON CONFLICT(path) DO UPDATE SET last_ts=excluded.last_ts",
                (file_path, ts),
            )
        finally:
            conn.close()

    def remaining_seconds(self, file_path: str, *, now: Optional[float] = None) -> float:
        """Seconds left on the cooldown for `file_path` (0.0 if not cooling)."""
        ts_now = time.time() if now is None else now
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT last_ts FROM file_cooldown WHERE path=?", (file_path,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return 0.0
        elapsed = ts_now - float(row["last_ts"])
        return max(0.0, self.cooldown_seconds - elapsed)

    def is_cooling(self, file_path: str, *, now: Optional[float] = None) -> bool:
        return self.remaining_seconds(file_path, now=now) > 0.0

    def try_claim(self, file_path: str, *, now: Optional[float] = None) -> float:
        """Atomically take the cooldown for `file_path`.

        Returns 0.0 if the claim was WON (the caller may proceed), else the
        seconds still remaining.

        This exists because `remaining_seconds()` followed by `record()` is a
        check-then-act: everything the caller does in between -- the audit
        write AND the actual file write -- happens inside a window where a
        second caller sees the same "free" answer. That window is the only
        rate limit on an agent rewriting its own system prompt, and under
        concurrency it stopped nothing: measured 2026-08-29, eight threads
        against one 24h lock applied 2, 3, 8, 7 and 8 of 8 across five runs,
        and six separate PROCESSES breached it in five runs of twelve.

        The whole decision is one conditional UPSERT, which sqlite executes as
        a single implicit transaction -- there is no window left to race in.
        The WHERE clause only lets the timestamp move forward once the
        cooldown has actually elapsed, so a losing caller changes nothing.
        """
        ts = time.time() if now is None else now
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO file_cooldown (path, last_ts) VALUES (?, ?) "
                "ON CONFLICT(path) DO UPDATE SET last_ts=excluded.last_ts "
                "WHERE excluded.last_ts - file_cooldown.last_ts >= ?",
                (file_path, ts, self.cooldown_seconds),
            )
            if cur.rowcount == 1:
                return 0.0
            row = conn.execute(
                "SELECT last_ts FROM file_cooldown WHERE path=?", (file_path,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return 0.0
        return max(0.0, self.cooldown_seconds - (ts - float(row["last_ts"])))
