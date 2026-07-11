# EXP-002 — Pre-registered predictions

**Committed 2026-07-11, before any measured run.** Immutable from this
commit forward: results publish either way. Design and method:
[exp-002-cadence-sweep.md](exp-002-cadence-sweep.md).

Context available when these were written: EXP-001's final N=5×3 result at
the single 4h/tick cadence (A: recall 0.940, 24.0 invocations mean; B/C:
recall 0.980, 42.0 invocations each). No EXP-002 data exists yet — the
`--tick-interval` harness parameter is not built. Every number below is a
prediction, not a measurement.

| # | Hypothesis | Prediction | Confidence |
|---|---|---|---|
| P1 | H1a — cron scaling | B and C invocation count equals tick count exactly at every cadence (7 / 42 / 168 / 672) — this is near-mechanical given cron calls once per tick by construction; the only way this fails is an implementation bug | 0.9 |
| P2 | H1b — A's cadence independence | Condition A's invocation count stays within 18–30 across all four cadences (matching its EXP-001 baseline band of 22–26), with no monotonic trend as cadence increases | 0.6 |
| P3 | H2 — widening absolute gap | The raw call-count gap (cron wakes − A wakes) at the fastest cadence (15min, 672 ticks) is at least 10× the gap measured at the 4h baseline (18) — i.e., gap ≥ 180 at very-fast vs. 18 at baseline | 0.7 |
| P4 | H3 — detection-latency tradeoff | At the 15min cadence, cron's mean time-to-detection (in hours) drops below condition A's fixed EXP-001 baseline TTD. This is the prediction most likely to go against the architecture — state plainly if it does | 0.55 |
| P5 | Corpus resolution ceiling | The very-fast (15min) cadence shows measurably less benefit over the fast (1h) cadence than fast shows over baseline (4h) — i.e., diminishing returns, likely because the underlying corpus's real event timestamps aren't resolved finer than roughly hourly | 0.5 |

## What would falsify the core bet

If condition A's invocation count grows with cadence at anywhere near the
rate cron's does (i.e., P2 fails badly — A is not actually
cadence-independent), the central cost claim from EXP-001 does not
generalize to realistic polling frequencies, and the "order of magnitude at
scale" framing in the EXP-001 design doc and TR-2026-03 needs to be
retracted, not just caveated.
