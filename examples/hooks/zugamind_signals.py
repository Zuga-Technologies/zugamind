#!/usr/bin/env python3
"""Example Claude Code hooks — feed the human side BACK into ZugaMind.

`zugamind_context.py` is the mind-to-session direction: findings injected
into your open session. This file is the session-to-mind direction: small,
cheap signals about what the human's Claude Code sessions are doing, written
where ZugaMind's scanners can see them. Together they close the loop.

Three modes, one per hook event:

    stop            fires when Claude finishes responding. Records a one-line
                    session pulse (cwd + a truncated gist of the last
                    assistant message — the payload carries it directly, no
                    transcript parsing needed). Also sweeps stale per-session
                    cursor files left by zugamind_context.py: SessionEnd is
                    NOT guaranteed to fire (verified empirically 2026-08-28 —
                    it never fired for a `claude -p` run), so cleanup is
                    opportunistic here instead of trusted to session teardown.
    session-end     fires when a session terminates (when it does fire).
                    Records the end + reason, deletes this session's cursor.
    notification    fires when Claude Code itself asks for attention
                    (waiting on permission, idle, an agent needing input).
                    For an attention sidecar this is a first-class signal:
                    "the human's own agent is blocked" is exactly the kind of
                    thing worth a salience bid.

Signals land as JSON lines in <data_dir>/engine/session_signals.jsonl —
deliberately NOT ZugaMind's own journal.jsonl: the journal is the engine's
own record, and two processes appending to it concurrently is a corruption
risk with no upside. The companion example scanner
(examples/custom-scanners/scan_session_signals.py) reads this feed and turns
unseen lines into workspace triggers, so signals compete for attention like
anything else.

Wire into `.claude/settings.json` (see this folder's README for the full
snippet):

    "Stop":         [{"hooks": [{"type": "command",
        "command": "python /path/to/zugamind_signals.py stop"}]}],
    "SessionEnd":   [{"hooks": [{"type": "command",
        "command": "python /path/to/zugamind_signals.py session-end"}]}],
    "Notification": [{"hooks": [{"type": "command",
        "command": "python /path/to/zugamind_signals.py notification"}]}]

Configuration (env):
    ZUGAMIND_DATA_DIR          where the feed lives (same var the rest of the
                               package uses). Defaults to the package's
                               zugamind/data — set explicitly if the hook
                               runs from a different cwd than the deployment.
    ZUGAMIND_HOOK_SIGNAL_TYPES comma-separated notification types to record.
                               Default: permission_prompt,idle_prompt,
                               agent_needs_input,agent_completed — the
                               attention-relevant ones; auth/quota chatter is
                               deliberately excluded.

The feed is bounded: past ~256KB it is rewritten keeping the newest 250
lines (atomic tmp+replace, so a crash mid-rewrite can't corrupt it). Cursor
files older than 14 days are removed on each stop-mode run.

Stdlib only. Fails silent and exits 0 on any error — a broken hook must
never block Claude Code. Payload fields verified against a real hook dump
on Claude Code 2.1.250-era (2026-08-28); every read is defensive anyway.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DATA_DIR = Path(os.environ.get("ZUGAMIND_DATA_DIR")
                  or Path(__file__).resolve().parent.parent.parent / "zugamind" / "data")
_FEED = _DATA_DIR / "engine" / "session_signals.jsonl"
_CURSOR_DIR = _DATA_DIR / "engine" / "hook_cursors"

_FEED_MAX_BYTES = 256 * 1024
_FEED_KEEP_LINES = 250
_CURSOR_MAX_AGE_SEC = 14 * 24 * 3600
_GIST_CHARS = 200

_DEFAULT_SIGNAL_TYPES = "permission_prompt,idle_prompt,agent_needs_input,agent_completed"


def _read_stdin_json() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gist(text: Any) -> str:
    """One line, bounded — a pulse, not a transcript."""
    return " ".join(str(text or "").split())[:_GIST_CHARS]


def _append_signal(record: dict[str, Any]) -> None:
    try:
        _FEED.parent.mkdir(parents=True, exist_ok=True)
        with open(_FEED, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        _bound_feed()
    except Exception:
        pass


def _bound_feed() -> None:
    """Keep the feed from growing forever; atomic rewrite of the newest tail."""
    try:
        if _FEED.stat().st_size <= _FEED_MAX_BYTES:
            return
        lines = _FEED.read_text(encoding="utf-8").splitlines()
        tmp = _FEED.with_suffix(".tmp")
        tmp.write_text("\n".join(lines[-_FEED_KEEP_LINES:]) + "\n", encoding="utf-8")
        tmp.replace(_FEED)
    except Exception:
        pass


def _sweep_stale_cursors() -> None:
    """SessionEnd is not guaranteed to fire, so age out zugamind_context.py's
    per-session cursor files here instead of trusting teardown."""
    try:
        cutoff = time.time() - _CURSOR_MAX_AGE_SEC
        for p in _CURSOR_DIR.glob("*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except Exception:
                continue
    except Exception:
        pass


def _mode_stop(payload: dict[str, Any]) -> None:
    # stop_hook_active means another Stop hook forced Claude to continue and
    # this firing is part of that continuation — skip to avoid double pulses.
    if payload.get("stop_hook_active"):
        return
    _append_signal({
        "ts": _now_iso(),
        "kind": "human_session_pulse",
        "session_id": str(payload.get("session_id") or ""),
        "cwd": str(payload.get("cwd") or ""),
        "gist": _gist(payload.get("last_assistant_message")),
    })
    _sweep_stale_cursors()


def _mode_session_end(payload: dict[str, Any]) -> None:
    session_id = str(payload.get("session_id") or "")
    _append_signal({
        "ts": _now_iso(),
        "kind": "human_session_end",
        "session_id": session_id,
        "cwd": str(payload.get("cwd") or ""),
        "reason": str(payload.get("reason") or ""),
    })
    try:
        safe_id = "".join(c for c in session_id if c.isalnum() or c in "-_")
        if safe_id:
            (_CURSOR_DIR / f"{safe_id}.json").unlink(missing_ok=True)
    except Exception:
        pass


def _mode_notification(payload: dict[str, Any]) -> None:
    wanted = {
        t.strip()
        for t in os.environ.get("ZUGAMIND_HOOK_SIGNAL_TYPES", _DEFAULT_SIGNAL_TYPES).split(",")
        if t.strip()
    }
    # Field name read defensively — the payload shape for Notification is the
    # least-settled of the three events.
    ntype = str(payload.get("notification_type") or payload.get("type") or "")
    if ntype not in wanted:
        return
    _append_signal({
        "ts": _now_iso(),
        "kind": "claude_attention",
        "type": ntype,
        "message": _gist(payload.get("message") or payload.get("notification_text")),
        "session_id": str(payload.get("session_id") or ""),
        "cwd": str(payload.get("cwd") or ""),
    })


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    mode = argv[0] if argv else ""
    payload = _read_stdin_json()
    try:
        if mode == "stop":
            _mode_stop(payload)
        elif mode == "session-end":
            _mode_session_end(payload)
        elif mode == "notification":
            _mode_notification(payload)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
