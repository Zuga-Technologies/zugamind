"""ZugaMind continuity journal — episodic event log + harness wake briefing.

An agent harness (Claude Code, OpenClaw, Hermes, ...) is stateless between
invocations: it thinks when prompted, then forgets. This journal is
ZugaMind's side of the continuity the harness itself can't hold — every
notable cycle event (a workspace winner, a harness invocation, an alarm, a
handoff) is appended here in order, and `build_briefing()` turns the tail of
that record into the markdown a waking harness reads on its way in.

Storage: an append-only JSONL file at `<ENGINE_DIR>/journal.jsonl` (one JSON
object per line, oldest first). Stdlib-only (json + pathlib + datetime).

Fail-closed on write: `append_event()` must never be the reason a cognitive
cycle crashes, so any disk/serialization error is logged and swallowed.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from foundation.config import ENGINE_DIR
from foundation.state import load_state

logger = logging.getLogger("zugamind.continuity.journal")

JOURNAL_FILE: Path = ENGINE_DIR / "journal.jsonl"

# How many raw journal lines build_briefing() scans to find recent notable
# events and to resolve handoff/handoff_done pairs. Generous on purpose —
# the journal is small and this is a local file read, not a network call.
_BRIEFING_SCAN_LIMIT = 2000

# How many items to list per group in the rendered briefing, before any
# hard-cap truncation kicks in — keeps the whole briefing well under the
# ~80-line budget regardless of journal size in the common case.
_GROUP_DISPLAY_CAP = 5

# Hard ceiling on the rendered briefing's length, in characters, overridable
# via ZUGAMIND_BRIEFING_MAX_CHARS. Context assembly is exactly where
# ambient-cognition systems go wrong: an unbounded "everything that
# happened" dump drowns the waking harness's own context window in noise
# instead of orienting it. build_briefing() enforces this cap itself rather
# than trusting every caller to truncate downstream.
_DEFAULT_BRIEFING_MAX_CHARS = 4000
_TRUNCATION_SUFFIX = "\n\n_(older events trimmed to fit ZUGAMIND_BRIEFING_MAX_CHARS)_"


def now_iso() -> str:
    """Current UTC time as an ISO 8601 string. A module-level seam so tests
    can monkeypatch it to backdate synthetic journal entries."""
    return datetime.now(timezone.utc).isoformat()


def append_event(kind: str, payload: Dict[str, Any]) -> None:
    """Append one event: `{"ts": now_iso(), "kind": kind, **payload}`.

    Best-effort and side-effect-free on failure: a full disk, a bad payload,
    or a permissions error is logged at WARNING and swallowed — journaling
    must never be the reason the caller's cycle breaks.
    """
    try:
        ENGINE_DIR.mkdir(parents=True, exist_ok=True)
        event = {"ts": now_iso(), "kind": kind, **payload}
        with JOURNAL_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")
    except Exception as e:  # noqa: BLE001 — journaling is best-effort, never fatal
        logger.warning("journal append failed (non-fatal): %s", e)


def read_events(
    since_iso: Optional[str] = None,
    limit: int = 200,
    *,
    on_error: str = "empty",
) -> List[Dict[str, Any]]:
    """Return journal events in chronological order (oldest first).

    Args:
        since_iso: if given, only events with `ts` strictly greater than
                   this ISO string are returned (both compare as strings —
                   safe because every `ts` is produced by `now_iso()`, which
                   always emits the same UTC-offset ISO format).
        limit: cap on the number of (post-filter) events returned; the most
               recent `limit` are kept. Malformed lines are skipped rather
               than raising. A missing journal file returns [].
        on_error: "empty" (default) degrades a *read failure* of an existing
                  file to [] — right for briefing/narrative callers, where no
                  history beats no cycle. "raise" re-raises it instead — for
                  callers whose SAFETY depends on distinguishing "no events"
                  from "couldn't read the events" (e.g. the rate limiter in
                  act/command_actuator, which must fail closed, not open,
                  when the count is unknowable). A missing file is [] in both
                  modes: a fresh install genuinely has no history.
    """
    if not JOURNAL_FILE.exists():
        return []
    try:
        raw_lines = JOURNAL_FILE.read_text(encoding="utf-8").splitlines()
    except Exception as e:  # noqa: BLE001 — see on_error
        if on_error == "raise":
            raise
        logger.warning("journal read failed (non-fatal): %s", e)
        return []

    events: List[Dict[str, Any]] = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if since_iso is not None and event.get("ts", "") <= since_iso:
            continue
        events.append(event)

    if limit is not None and limit >= 0:
        events = events[-limit:]
    return events


def _describe_elapsed(since_iso: str, now: Optional[datetime] = None) -> str:
    """Human-readable elapsed time between `since_iso` and `now` (defaults
    to the real current time — injectable so briefings are testable)."""
    try:
        then = datetime.fromisoformat(since_iso)
        current = now if now is not None else (
            datetime.now(timezone.utc) if then.tzinfo else datetime.now()
        )
        delta = current - then
        total_minutes = max(0, int(delta.total_seconds() // 60))
        hours, minutes = divmod(total_minutes, 60)
        if hours:
            return f"{hours}h {minutes}m"
        if total_minutes:
            return f"{total_minutes}m"
        return "<1m"
    except Exception:
        return "unknown"


def _max_briefing_chars() -> int:
    try:
        return int(os.environ.get("ZUGAMIND_BRIEFING_MAX_CHARS", _DEFAULT_BRIEFING_MAX_CHARS))
    except (TypeError, ValueError):
        return _DEFAULT_BRIEFING_MAX_CHARS


def build_briefing(
    since_iso: Optional[str],
    winner: Optional[Dict[str, Any]] = None,
    *,
    now: Optional[datetime] = None,
) -> str:
    """Render a markdown wake briefing for a harness invocation.

    Args:
        since_iso: ISO timestamp of the last wake (the journal cursor), or
                   None if this is the first briefing ever built. Also used
                   to compute "time since last wake".
        winner: the workspace winner that triggered THIS wake — typically
                `WorkspaceContent.to_dict()` — or None for a non-winner-
                triggered wake (e.g. a scheduled/manual run).
        now: override for "the current time", for deterministic tests. Real
             callers should omit it.

    Sections: current cognitive state + time since last wake; the winning
    trigger that caused this wake; recent notable events since the last
    wake grouped into winners / actions taken / alarms / deferred-during-
    quiet-hours; and unresolved handoffs (kind "handoff" events with no
    matching "handoff_done").

    HARD SIZE CAP: the rendered briefing is truncated to at most
    `ZUGAMIND_BRIEFING_MAX_CHARS` characters (default 4000). Context
    assembly is exactly where "ambient cognition" implementations tend to
    go wrong — an unbounded dump of everything that happened between wakes
    drowns the waking harness's own context window in noise instead of
    orienting it. When the natural render is too long, the OLDEST displayed
    item in each group is dropped first (one round at a time, across all
    groups together, since groups are already newest-last); the current
    winner's "Why you're being woken" section is never touched — it's the
    one piece of context this briefing exists to deliver.

    Deterministic given the journal's contents and `now`: no randomness, no
    hidden clock reads beyond the one explicit `now` (or, if omitted, a
    single read of the real clock for the elapsed-time line).
    """
    try:
        state = load_state()
    except Exception as e:  # noqa: BLE001 — a broken state file must not break the briefing
        logger.debug("briefing: state load failed: %s", e)
        state = {"state": "UNKNOWN"}

    all_events = read_events(since_iso=None, limit=_BRIEFING_SCAN_LIMIT)
    recent_events = [e for e in all_events if since_iso is None or e.get("ts", "") > since_iso]

    winners = [e for e in recent_events if e.get("kind") == "cycle" and e.get("winner")]
    actions = [e for e in recent_events if e.get("kind") == "harness_invocation"]
    alarms = [e for e in recent_events if e.get("kind") == "alarm"]
    deferred = [e for e in recent_events if e.get("kind") == "quiet_hours_deferred"]

    handoff_done_ids = {
        e.get("id") for e in all_events if e.get("kind") == "handoff_done" and e.get("id")
    }
    unresolved = [
        e for e in all_events
        if e.get("kind") == "handoff" and e.get("id") not in handoff_done_ids
    ]

    def _render(group_cap: int) -> str:
        lines: List[str] = ["# ZugaMind Wake Briefing", ""]
        lines.append(f"**Cognitive state:** {state.get('state', 'UNKNOWN')}")
        if since_iso:
            lines.append(f"**Time since last wake:** {_describe_elapsed(since_iso, now)}")
        else:
            lines.append("**Time since last wake:** (no prior wake recorded — first briefing)")

        lines.append("")
        lines.append("## Why you're being woken")
        if winner:
            module = winner.get("source_module", "?")
            content = str(winner.get("content", ""))[:200]
            salience = winner.get("salience")
            sal_str = f"{salience:.2f}" if isinstance(salience, (int, float)) else "?"
            lines.append(f"- **{module}** (salience {sal_str}): {content}")
        else:
            lines.append("- (no winner supplied — scheduled/manual wake)")

        lines.append("")
        lines.append("## Since last wake")
        lines.append(
            f"- {len(winners)} workspace winner(s), {len(actions)} harness invocation(s), "
            f"{len(alarms)} alarm(s), {len(deferred)} deferred (quiet hours)"
        )

        if group_cap > 0 and winners:
            lines.append("")
            lines.append("### Winners")
            for e in winners[-group_cap:]:
                w = e.get("winner") or {}
                lines.append(f"- [{e.get('ts', '?')}] {w.get('source_module', '?')}: "
                             f"{str(w.get('content', ''))[:120]}")

        if group_cap > 0 and actions:
            lines.append("")
            lines.append("### Actions taken")
            for e in actions[-group_cap:]:
                status = "ok" if e.get("ok") else "FAILED"
                dry = " (dry-run)" if e.get("dry_run") else ""
                lines.append(f"- [{e.get('ts', '?')}] {e.get('harness', '?')}: {status}{dry}")

        if group_cap > 0 and alarms:
            lines.append("")
            lines.append("### Alarms")
            for e in alarms[-group_cap:]:
                lines.append(f"- [{e.get('ts', '?')}] {e.get('detail', e.get('reason', '?'))}")

        if group_cap > 0 and deferred:
            lines.append("")
            lines.append("### Deferred during quiet hours")
            for e in deferred[-group_cap:]:
                w = e.get("winner") or {}
                lines.append(f"- [{e.get('ts', '?')}] {e.get('harness', '?')} <- "
                             f"{w.get('source_module', '?')}: {str(w.get('content', ''))[:100]}")

        lines.append("")
        lines.append("## Unresolved handoffs")
        if group_cap > 0 and unresolved:
            for e in unresolved[-group_cap:]:
                lines.append(f"- [{e.get('ts', '?')}] {e.get('id', '?')}: {e.get('detail', '')}")
        else:
            lines.append("- none" if not unresolved else f"- {len(unresolved)} pending (trimmed — see journal)")

        return "\n".join(lines)

    max_chars = _max_briefing_chars()
    cap = _GROUP_DISPLAY_CAP
    text = _render(cap)
    while len(text) > max_chars and cap > 0:
        cap -= 1
        text = _render(cap)

    if len(text) > max_chars:
        # Should only happen if the protected header itself is huge (e.g. an
        # absurd winner content) — hard-slice as the last resort, but the
        # winner's own content is already capped to 200 chars above.
        text = text[: max(0, max_chars - len(_TRUNCATION_SUFFIX))] + _TRUNCATION_SUFFIX

    return text


__all__ = ["append_event", "read_events", "build_briefing", "now_iso", "JOURNAL_FILE"]
