#!/usr/bin/env python3
"""Example Claude Code hook — feed ZugaMind's findings into a session as
context, not as an interruption.

Not part of the ZugaMind package. This is a worked example of the OTHER
half of the wake path: instead of (or alongside) ZugaMind spawning a
brand-new headless session when something crosses its attention floor,
this hook lets an ALREADY-OPEN Claude Code session pick up whatever
ZugaMind has found since you last looked — automatically, at the start of
a session or on your next prompt, without you asking.

What this does NOT do: interrupt you mid-session while you're doing
nothing. Claude Code hooks fire on Claude Code's own lifecycle events
(a session starting, you submitting a prompt) — not on a timer, not the
instant a scanner fires. This surfaces on the next thing you type or the
next session you open, not the second something happens. That's an
honest tradeoff, not a bug.

Supports two hook events, each with its OWN correct output contract
(they are NOT interchangeable — verify against your Claude Code version
if this ever stops matching):

    SessionStart        stdout is JSON:
                         {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                  "additionalContext": "..."}}
    UserPromptSubmit     stdout is PLAIN TEXT, prepended as additional
                         context before your actual prompt.

Wire it into your own project's `.claude/settings.json`:

    {
      "hooks": {
        "SessionStart": [{"hooks": [{"type": "command",
            "command": "python /path/to/zugamind_context.py session-start"}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command",
            "command": "python /path/to/zugamind_context.py user-prompt-submit"}]}]
      }
    }

Configuration (env):
    ZUGAMIND_DATA_DIR    where journal.jsonl lives (same var the rest of
                         the package uses). Defaults to ./data relative
                         to wherever ZugaMind itself is installed — set
                         this explicitly if your hook runs from a
                         different cwd than your ZugaMind deployment.
    ZUGAMIND_HOOK_MAX_ITEMS   cap on how many new findings to surface in
                              one injection. Default 3 — this is meant to
                              be a nudge, not a dump.

Cursor (what counts as "new since last time") is a byte offset into
journal.jsonl, persisted PER SESSION to
<data_dir>/engine/hook_cursors/<session_id>.json — one cursor file per
open session, not one shared file. This matters: with a single shared
cursor, two sessions open at once would race — whichever sends a prompt
first consumes the finding and the second session's next prompt would
silently find nothing, even though it never actually saw it. Per-session
cursors mean every open session gets every finding on its own next
prompt, independent of what any other open session already consumed.
`session_id` comes from the hook's own stdin payload (Claude Code always
includes it); a payload missing it (shouldn't happen, defensive only)
falls back to a shared `_default.json` cursor rather than crashing.

Stdlib only. Fails silent and exits 0 on any error — a broken hook must
never block you from using Claude Code.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_DATA_DIR = Path(os.environ.get("ZUGAMIND_DATA_DIR")
                  or Path(__file__).resolve().parent.parent.parent / "zugamind" / "data")
_JOURNAL = _DATA_DIR / "engine" / "journal.jsonl"
_CURSOR_DIR = _DATA_DIR / "engine" / "hook_cursors"
_MAX_ITEMS = int(os.environ.get("ZUGAMIND_HOOK_MAX_ITEMS", "3"))

_INTERESTING_KINDS = {"harness_invocation", "alarm"}


def _read_stdin_json() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def _cursor_file(session_id: str) -> Path:
    safe_id = "".join(c for c in session_id if c.isalnum() or c in "-_") or "_default"
    return _CURSOR_DIR / f"{safe_id}.json"


def _load_cursor(cursor_file: Path) -> int:
    try:
        if cursor_file.exists():
            return int(json.loads(cursor_file.read_text(encoding="utf-8")).get("offset", 0))
    except Exception:
        pass
    return 0


def _save_cursor(cursor_file: Path, offset: int) -> None:
    try:
        cursor_file.parent.mkdir(parents=True, exist_ok=True)
        cursor_file.write_text(json.dumps({"offset": offset}), encoding="utf-8")
    except Exception:
        pass


def _scan_from(offset: int) -> tuple[list[dict], int]:
    findings: list[dict] = []
    with open(_JOURNAL, encoding="utf-8") as f:
        f.seek(offset)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("kind") in _INTERESTING_KINDS:
                findings.append(ev)
        new_offset = f.tell()
    return findings, new_offset


def _new_findings(cursor_file: Path) -> tuple[list[dict], int]:
    """Return (findings since this session's saved cursor, new cursor offset).

    First-ever prompt in THIS session (no cursor file yet): shows a bounded
    "catch-up" of the last _MAX_ITEMS findings in the whole journal,
    regardless of when they happened — a session opened AFTER something was
    found should still see it. This is a full scan from offset 0 capped to
    the tail, not "everything since the beginning" (that's what "bounded"
    buys you: recency, not a full-history dump). Every later prompt in this
    same session only sees genuinely NEW findings since its own last check.
    """
    if not _JOURNAL.exists():
        return [], 0

    size = _JOURNAL.stat().st_size
    if not cursor_file.exists():
        all_findings, _ = _scan_from(0)
        return all_findings[-_MAX_ITEMS:], size

    offset = _load_cursor(cursor_file)
    if offset > size:
        offset = 0  # journal was rotated/truncated — start fresh rather than crash

    return _scan_from(offset)


def _format_findings(findings: list[dict]) -> str:
    lines = []
    for ev in findings[:_MAX_ITEMS]:
        kind = ev.get("kind")
        ts = ev.get("ts", "")
        if kind == "alarm":
            lines.append(f"- [{ts}] alarm: {ev.get('detail', '')[:200]}")
        elif kind == "harness_invocation":
            stdout = (ev.get("stdout") or "").strip().replace("\n", " ")
            lines.append(f"- [{ts}] wake result ({ev.get('harness', '?')}): {stdout[:250]}")
    if len(findings) > _MAX_ITEMS:
        lines.append(f"- (+{len(findings) - _MAX_ITEMS} more since last check)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    mode = argv[0] if argv else "user-prompt-submit"
    payload = _read_stdin_json()

    # Don't fire on non-human events (task notifications, system blocks) —
    # same guard this pattern uses elsewhere; avoids noisy false injections.
    prompt = (payload.get("prompt") or "")
    if mode == "user-prompt-submit" and (
        "<task-notification>" in prompt or "[SYSTEM NOTIFICATION" in prompt
    ):
        return 0

    session_id = str(payload.get("session_id") or "")
    cursor_file = _cursor_file(session_id)

    findings, new_offset = _new_findings(cursor_file)
    _save_cursor(cursor_file, new_offset)
    if not findings:
        return 0

    body = _format_findings(findings)
    context = (
        f"ZugaMind found {len(findings)} thing(s) since you last checked "
        f"(background, unprompted):\n{body}\n"
        f"Use this if relevant to what we're working on — no need to "
        f"mention it if it isn't."
    )

    if mode == "session-start":
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        }))
    else:
        sys.stdout.write(f"<system-reminder source=\"zugamind\">\n{context}\n</system-reminder>\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
