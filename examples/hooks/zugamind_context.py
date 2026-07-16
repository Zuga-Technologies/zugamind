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
journal.jsonl, persisted to <data_dir>/engine/hook_cursor.json. Stdlib
only. Fails silent and exits 0 on any error — a broken hook must never
block you from using Claude Code.
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
_CURSOR_FILE = _DATA_DIR / "engine" / "hook_cursor.json"
_MAX_ITEMS = int(os.environ.get("ZUGAMIND_HOOK_MAX_ITEMS", "3"))

_INTERESTING_KINDS = {"harness_invocation", "alarm"}


def _read_stdin_json() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def _load_cursor() -> int:
    try:
        if _CURSOR_FILE.exists():
            return int(json.loads(_CURSOR_FILE.read_text(encoding="utf-8")).get("offset", 0))
    except Exception:
        pass
    return 0


def _save_cursor(offset: int) -> None:
    try:
        _CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CURSOR_FILE.write_text(json.dumps({"offset": offset}), encoding="utf-8")
    except Exception:
        pass


def _new_findings() -> tuple[list[dict], int]:
    """Return (findings since the saved cursor, new cursor offset).

    On first-ever run (no cursor file) the cursor is seeded to the
    CURRENT end of the journal rather than 0 — otherwise the very first
    session after wiring this hook up would dump the entire history.
    """
    if not _JOURNAL.exists():
        return [], 0

    size = _JOURNAL.stat().st_size
    if not _CURSOR_FILE.exists():
        _save_cursor(size)
        return [], size

    offset = _load_cursor()
    if offset > size:
        offset = 0  # journal was rotated/truncated — start fresh rather than crash

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


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "user-prompt-submit"
    payload = _read_stdin_json()

    # Don't fire on non-human events (task notifications, system blocks) —
    # same guard this pattern uses elsewhere; avoids noisy false injections.
    prompt = (payload.get("prompt") or "")
    if mode == "user-prompt-submit" and (
        "<task-notification>" in prompt or "[SYSTEM NOTIFICATION" in prompt
    ):
        return 0

    findings, new_offset = _new_findings()
    _save_cursor(new_offset)
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
