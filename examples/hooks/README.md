# Feeding ZugaMind into an already-open session

The rest of this repo is about ZugaMind spawning a **new, separate,
headless** session when something crosses its attention floor. This
directory is the other half: letting a session you're **already working
in** pick up whatever ZugaMind has found since you last looked — as
context, automatically, without you asking.

## What this actually does

- **`SessionStart`** — every time a new Claude Code session opens, check
  for anything new since last time and inject a short summary.
- **`UserPromptSubmit`** — same check, but on every message you send in a
  session that's already open, so it stays current as you keep working.

## What it does NOT do

It does not interrupt you mid-session while you're doing nothing. Claude
Code hooks fire on Claude Code's own lifecycle events (a session
starting, you submitting a prompt) — not on a timer, not the instant a
scanner fires. This surfaces on the **next** thing you type or the next
session you open. That's the honest tradeoff: no true push notifications,
but zero extra infrastructure, and it uses a mechanism Claude Code
already ships.

## Multiple sessions open at once

Each session gets its own cursor, keyed by `session_id`. If you have two
sessions open and a finding lands, BOTH sessions surface it independently
on their own next prompt — not just whichever asks first. A brand-new
session's first-ever prompt shows a bounded catch-up of recent findings
(not the full history), even for things that happened before that
session existed — a session opened after a finding still sees it.

## Setup

1. Copy `zugamind_context.py` into your own project (or reference it by
   absolute path — it's self-contained, stdlib only).
2. Add to your project's `.claude/settings.json`:

   ```json
   {
     "hooks": {
       "SessionStart": [
         {"hooks": [{"type": "command",
           "command": "python /path/to/zugamind_context.py session-start"}]}
       ],
       "UserPromptSubmit": [
         {"hooks": [{"type": "command",
           "command": "python /path/to/zugamind_context.py user-prompt-submit"}]}
       ]
     }
   }
   ```

3. Set `ZUGAMIND_DATA_DIR` if your hook runs from a different working
   directory than your ZugaMind deployment (same env var the rest of the
   package uses).

That's it — no daemon changes needed. The hook reads `journal.jsonl`
directly; it doesn't need ZugaMind's process to be aware of it at all.

## Verifying it end to end

```bash
export ZUGAMIND_DATA_DIR=/path/to/your/zugamind/data
echo '{"prompt":"test"}' | python zugamind_context.py user-prompt-submit
```

First run seeds a cursor and prints nothing (so wiring this up doesn't
dump your entire wake history into your next session). Trigger a real
wake, run the command again, and you should see a `<system-reminder>`
block with what it found.
