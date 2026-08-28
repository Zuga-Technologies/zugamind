# EXP-003 tier-1 — FIRST ATTEMPT, INVALID (2026-07-11)

These 10 runs (A×5, D×5) are **not valid measurements** and are kept only as
the raw record, per the publish-everything discipline. Every run scored 0.0
recall on both planted items in both conditions — an artifact of the run
setup, not a result. Three independent operator errors compounded:

1. **Wrong harness config.** The runs were launched against a generic
   `claude-code` config instead of an EXP-003 version of
   `scripts/exp001_claude_config.json`. The prompt carried no ACT-protocol
   triage instruction, so the model never emitted `ACT: <item-id>` lines and
   the deterministic grader had nothing to match — even on wakes where the
   briefing did contain the planted item.
2. **Rate limiting.** That config carried the default `max_per_hour: 4`.
   The rate limiter counts wall-clock time, and a 42-tick simulated week
   replays in seconds — so all but the first few invocations per run were
   refused (`"error": "rate_limited"` in the journals). The buried signal's
   wake at e.g. A-run0 tick 28 was one of the refused calls.
3. **Hook-blocked harness sessions.** The woken `claude -p` sessions
   inherited a PreToolUse hook pointing at a per-project script that does
   not exist in this repo's checkout, which hard-blocked every tool call —
   several transcripts show the model unable to even read the briefing file
   (visible verbatim in the journal `harness_invocation` events).

The A-vs-D invocation asymmetry in these runs (~9-11 vs ~40-42 attempted
wakes) is still mechanically informative — condition D, with the health
layer off, attempted a wake nearly every tick because the dominant chatter
kept clearing the salience floor un-dampened — but no recall/detection
number from this directory should be quoted anywhere.

The valid tier-1 runs live in `exp003-tier1/`, executed with
`scripts/exp003_claude_config.json` (caps effectively disabled, EXP-003 ACT
prompt) after all three faults were fixed.
