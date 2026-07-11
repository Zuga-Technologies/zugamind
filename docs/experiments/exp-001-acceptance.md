# EXP-001 — Post-fix acceptance runs (issues #8, #9)

Two acceptance passes of condition A (5 runs each, frozen corpus, same
seeds as the measured Tier-1 runs), gate on the local tier
(`ZUGAMIND_WAKE_TIER=local`, qwen2.5:7b-instruct judges the wake — the
"$0 idle loop"), harness `claude -p` (sonnet pinned). Raw runs:
`exp001-accept-A/`.

| stage | recall (5 runs) | precision | false acts | wakes/run | TTD mean |
|---|---|---|---|---|---|
| baseline (pre-fix Tier-1) | 0.94 | 1.00 | 0 | 24.0 | 0.34 |
| alarm lane + briefing enumeration only | 0.92 | 1.00 | 0 | 24.0 | 0.13 |
| + critical digest | **0.98** | 1.00 | 0 | 23.8 | **0.10** |

Reference: cron (conditions B/C) scored 0.98 recall at 42 invocations/run.

## What the intermediate stage taught

The alarm lane alone did NOT raise recall: with 4-5 overlapping alarm
windows and one wake slot per tick, fair rotation still drops whoever
expires first — queueing capacity, not selection policy. Detection
latency improved (0.34 -> 0.13 ticks) and behavior became deterministic,
but misses just moved between canaries.

The fix that closed the gap mirrors real pagers: the **critical digest**
(commit history: "Other active alarms" briefing section) rides every
currently-active critical bid along on whichever wake fires, so
concurrent alarms no longer compete for slots at all.

## Verdict

Both issue acceptance criteria met on the second pass: aggregate recall
0.94 -> 0.98 (now equal to cron's), zero new false acts, wakes unchanged.
The remaining miss is 1 canary in 50 across the three A-condition
batches; no single canary misses repeatedly post-fix.

Post-fix headline: **equal detection to cron, 43% fewer model
invocations, 3x faster in-tick delivery, zero false alarms — with the
wake decision itself running on a free local model.**
