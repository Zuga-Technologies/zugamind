# Claude Code hooks for ZugaMind — both directions

The rest of this repo is about ZugaMind spawning a **new, separate,
headless** session when something crosses its attention floor. This
directory closes the loop with the sessions you're **already working in**,
in both directions:

```
mind -> session   zugamind_context.py    what ZugaMind found, injected as
                                         context when you open a session or
                                         send a prompt
session -> mind   zugamind_signals.py    what your sessions are doing —
                                         a per-turn pulse, "Claude is
                                         waiting on you" notifications,
                                         session endings — written where
                                         ZugaMind's scanners can see them
consumer          ../custom-scanners/scan_session_signals.py
                                         turns those signals into workspace
                                         triggers, so "the human's agent is
                                         blocked" competes for salience
                                         like any external event
```

## zugamind_context.py — mind → session

- **`SessionStart`** — every time a session opens, inject a short summary of
  anything new since last time. With no matcher configured this fires for
  every start source — including `compact`, which is the officially
  documented way context survives compaction (no `PreCompact` hook needed).
- **`UserPromptSubmit`** — same check on every message you send, so an open
  session stays current.

Each session gets its own cursor into ZugaMind's journal, keyed by
`session_id` — two open sessions BOTH surface a finding independently, and a
brand-new session's first prompt gets a bounded catch-up, not a history
dump. Full mechanics in the file's docstring.

## zugamind_signals.py — session → mind

- **`Stop`** — when Claude finishes responding, append a one-line pulse
  (cwd + truncated gist of the last assistant message — the payload carries
  it directly) to `<data_dir>/engine/session_signals.jsonl`. Also sweeps
  cursor files older than 14 days: **`SessionEnd` is not guaranteed to
  fire** (verified empirically 2026-08-28: it never fired for a `claude -p`
  run), so cleanup is opportunistic here, not trusted to teardown.
- **`SessionEnd`** — when it does fire: record the ending + reason, delete
  that session's cursor.
- **`Notification`** — when Claude Code itself asks for attention
  (`permission_prompt`, `idle_prompt`, `agent_needs_input`,
  `agent_completed` — filterable via `ZUGAMIND_HOOK_SIGNAL_TYPES`), record
  it. For an attention sidecar, "the human's own agent is blocked" is a
  first-class signal.

Signals go to their own feed file, deliberately NOT ZugaMind's
`journal.jsonl` — the journal is the engine's own record, and concurrent
appends from a second process are a corruption risk with no upside. The
feed is bounded (rewritten to the newest ~250 lines past 256KB, atomically).

## What this does NOT do

Hooks fire on Claude Code's own lifecycle events — a session starting, you
submitting a prompt, Claude finishing a turn — not on a timer, not the
instant a scanner fires. Mind-to-session context surfaces on your **next**
prompt; session-to-mind signals reach the workspace on the scanner's next
cycle. No true push, but zero extra infrastructure, using mechanisms Claude
Code already ships.

## Setup

1. Copy the `.py` files somewhere stable (or reference them in place —
   self-contained, stdlib only).
2. Add to your project's `.claude/settings.json` (hook arrays MERGE across
   settings levels, so this runs alongside anything you already have):

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
       ],
       "Stop": [
         {"hooks": [{"type": "command",
           "command": "python /path/to/zugamind_signals.py stop"}]}
       ],
       "SessionEnd": [
         {"hooks": [{"type": "command",
           "command": "python /path/to/zugamind_signals.py session-end"}]}
       ],
       "Notification": [
         {"hooks": [{"type": "command",
           "command": "python /path/to/zugamind_signals.py notification"}]}
       ]
     }
   }
   ```

3. Set `ZUGAMIND_DATA_DIR` if your hooks run from a different working
   directory than your ZugaMind deployment (same env var the rest of the
   package uses). Both hook files and the scanner resolve the feed through
   it, so setting it once aligns all three.
4. To close the loop, wire the consumer into your launcher's
   `extra_scanners` (see `../custom-scanners/README.md`):

   ```python
   from scan_session_signals import scan_session_signals
   runner = StreamRunner(extra_scanners={
       "scan_session_signals": scan_session_signals,
   })
   ```

That's it — no daemon changes. The context hook reads `journal.jsonl`
directly; the signals hook writes its own feed; ZugaMind's process needs no
awareness of either.

## Design rules these hooks follow (steal them for your own)

- **Exit 0 always, fail silent** — a broken hook must never block Claude
  Code. Every error path swallows and returns.
- **Nudge, not dump** — context injections cap at 3 items; signal gists cap
  at 200 chars; the feed self-bounds.
- **Output contracts differ per event** and are NOT interchangeable:
  `SessionStart` wants JSON `hookSpecificOutput`; `UserPromptSubmit` wants
  plain stdout; `Stop`/`SessionEnd`/`Notification` output nothing at all.
- **Atomic state writes** (tmp + `os.replace`) — a kill mid-write must not
  corrupt a cursor or the feed.
- **Payloads read defensively** — every field via `.get()` with fallbacks.
  Field shapes were verified against real payload dumps on a 2.1.250-era
  CLI (2026-08-28), but treat any hook payload as version-dependent.

## Verifying end to end

```bash
export ZUGAMIND_DATA_DIR=/path/to/your/zugamind/data

# mind -> session (first run seeds a cursor and prints nothing):
echo '{"prompt":"test"}' | python zugamind_context.py user-prompt-submit

# session -> mind:
echo '{"session_id":"t1","cwd":"/tmp","last_assistant_message":"hello"}' \
  | python zugamind_signals.py stop
tail -1 "$ZUGAMIND_DATA_DIR/engine/session_signals.jsonl"
```

Tests live in `tests/examples/test_session_hooks.py`.

## Why not a Claude Code plugin?

Plugins are the right channel once a pack is big enough to version and
distribute (`hooks/hooks.json` accepts this exact JSON, so migration is a
copy-paste). For now this stays in the repo's example style — self-contained
files you read before you run — per the same philosophy as
`harness-configs/` shipping `enabled:false`. Revisit if the pack grows.
