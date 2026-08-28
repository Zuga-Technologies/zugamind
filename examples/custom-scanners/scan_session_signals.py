"""Example scanner — turn Claude Code session signals into workspace triggers.

The consumer half of examples/hooks/zugamind_signals.py: that hook writes
JSON lines about the human's Claude Code sessions (a per-turn pulse, an
attention request like "waiting on permission", a session ending) to
<data_dir>/engine/session_signals.jsonl. This scanner reads lines it hasn't
seen yet and turns them into triggers, so "the human's own agent is blocked"
can compete for salience like any external event.

Salience shape, deliberate:
  - claude_attention (permission_prompt / idle_prompt / agent_needs_input):
    urgent-ish — a human's agent is blocked RIGHT NOW.
  - claude_attention (agent_completed): mild — good to know, rarely wake-worthy.
  - human_session_pulse / human_session_end: ambient awareness — low scores,
    normally below any sane wake floor. They exist so the workspace can KNOW
    the human is active, not so it wakes because of it.

Dedupe is a byte-offset cursor into the feed (like tail -f), persisted to
data/scanner_cache/session_signals_state.json — no seen-set needed; a line
is consumed exactly once. The hook bounds/rewrites the feed when it grows,
so a stored offset can exceed the file size — that is detected and treated
as "start from the beginning of the rewritten file", never a crash.

Config (env):
    ZUGAMIND_SESSION_SIGNALS_FEED   override the feed path. Default: the
        hook's own default — <package>/zugamind/data/engine/
        session_signals.jsonl, or $ZUGAMIND_DATA_DIR/engine/... when set.
        NOTE: this default matches the HOOK's data-dir convention (package
        data dir), not this folder's scanner-cache convention — the two
        sides must agree on where the feed lives or they never meet.

Stdlib only. Fail-silent: any error returns [].
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DATA_DIR = Path(os.environ.get("ZUGAMIND_DATA_DIR") or Path(__file__).resolve().parent / "data")
_CACHE_DIR = _DATA_DIR / "scanner_cache"
_STATE_FILE = _CACHE_DIR / "session_signals_state.json"

_PKG_DATA_DIR = Path(os.environ.get("ZUGAMIND_DATA_DIR")
                      or Path(__file__).resolve().parent.parent.parent / "zugamind" / "data")
_FEED = Path(os.environ.get("ZUGAMIND_SESSION_SIGNALS_FEED")
             or _PKG_DATA_DIR / "engine" / "session_signals.jsonl")

_MAX_TRIGGERS = 5

# (novelty, relevance, urgency) per signal shape — see module docstring.
_ATTENTION_URGENT = {"permission_prompt", "idle_prompt", "agent_needs_input"}


def _load_offset() -> int:
    try:
        if _STATE_FILE.exists():
            return int(json.loads(_STATE_FILE.read_text(encoding="utf-8")).get("offset", 0))
    except Exception:
        pass
    return 0


def _save_offset(offset: int) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # tmp + replace: a crash mid-write must not leave truncated JSON
        # (os.replace is atomic on both POSIX and Windows).
        tmp = _STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"offset": offset}), encoding="utf-8")
        tmp.replace(_STATE_FILE)
    except Exception as e:
        logger.debug("session_signals state save failed: %s", e)


def _trigger_for(ev: dict[str, Any]) -> dict[str, Any] | None:
    kind = ev.get("kind")
    if kind == "claude_attention":
        ntype = str(ev.get("type") or "")
        urgent = ntype in _ATTENTION_URGENT
        detail = f"[claude-code] {ntype}: {ev.get('message') or '(no message)'}"
        return {
            "type": "claude_needs_human" if urgent else "claude_agent_update",
            "detail": detail[:280],
            "novelty": 0.6,
            "relevance": 0.8 if urgent else 0.5,
            "urgency": 0.7 if urgent else 0.3,
        }
    if kind == "human_session_pulse":
        cwd = str(ev.get("cwd") or "")
        return {
            "type": "human_activity",
            "detail": f"[claude-code] human session active in {cwd}: {ev.get('gist') or ''}"[:280],
            "novelty": 0.3,
            "relevance": 0.4,
            "urgency": 0.1,
        }
    if kind == "human_session_end":
        return {
            "type": "human_activity",
            "detail": f"[claude-code] session ended ({ev.get('reason') or '?'}) in {ev.get('cwd') or ''}"[:280],
            "novelty": 0.2,
            "relevance": 0.3,
            "urgency": 0.1,
        }
    return None


def scan_session_signals() -> list[dict[str, Any]]:
    """Return triggers for Claude Code session signals not yet consumed."""
    try:
        if not _FEED.exists():
            return []
        size = _FEED.stat().st_size
        offset = _load_offset()
        if offset > size:
            offset = 0  # feed was bounded/rewritten by the hook — restart

        triggers: list[dict[str, Any]] = []
        with open(_FEED, encoding="utf-8") as f:
            f.seek(offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                trig = _trigger_for(ev if isinstance(ev, dict) else {})
                if trig is not None and len(triggers) < _MAX_TRIGGERS:
                    triggers.append(trig)
            # Cursor always advances to end-of-file: signals past the trigger
            # cap this cycle are dropped, not replayed — a pulse from an hour
            # ago is stale awareness, unlike a scanner whose items stay
            # meaningful (agent_reach holds capped items back on purpose;
            # this feed's value decays too fast for that to help).
            new_offset = f.tell()

        _save_offset(new_offset)
        return triggers
    except Exception as e:
        logger.debug("scan_session_signals failed: %s", e)
        return []
