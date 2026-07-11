# EXP-003 — Pre-registered predictions

**Committed 2026-07-11, before any measured run.** Immutable from this
commit forward: results publish either way — this experiment exists
specifically because we could not defend the attention-health layer's
value with data when asked directly, so a null result here is a genuine,
expected possible outcome, not a surprise to be explained away. Design and
method: [exp-003-attention-health-ablation.md](exp-003-attention-health-ablation.md).

Context available when these were written: no EXP-003 data exists. Neither
the `attention_health_enabled` flag nor the dominant/buried corpus builder
is built yet. Every number below is a prediction.

| # | Hypothesis | Prediction | Confidence |
|---|---|---|---|
| P1 | H1 — starvation resistance | Condition A's recall on the buried signal ≥ 0.8 across N=5 runs; condition D's (bare max-salience, no health corrections) recall on the same signal ≤ 0.6 — gap ≥ 20 percentage points | 0.5 |
| P2 | H2 — no free lunch on the dominant source's own real alert | Condition A's recall on the dominant-source's planted real alert is NOT lower than condition D's on the same — i.e., A's streak dampening (which targets the dominant source specifically) does not accidentally suppress a genuine alert from that same source. This is the least certain prediction in the set: dampening is not alert-aware, so it plausibly could suppress real and noise signals from the same source equally | 0.45 |
| P3 | H3 — magnitude, practical significance | The P1 gap, if it exists in the predicted direction, is ≥15 percentage points (the pre-registered practical-significance threshold from the design doc) — a gap smaller than this is treated as "no meaningful effect" even if directionally positive | tied to P1 |
| P4 | Invocation count parity | Condition A and D have similar total invocation counts (within ±3) — total event volume is identical between conditions; only *which* items win differs, not how many wakes occur | 0.7 |

## What would falsify the core bet

If condition D matches condition A's buried-signal recall (gap < 10
percentage points), the attention-health layer is not earning its
complexity for this failure mode, and TR-2026-03 and any related outreach
say exactly that — the architecture would need a different, evidenced
justification, or an honest acknowledgment that its main current value is
the cost result from EXP-001, not superior arbitration quality.
