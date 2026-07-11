# EXP-002 — Cadence sweep: does the cost gap widen with polling frequency?

**Status:** DESIGNED, not yet run. Predictions will be pre-registered (see
[Pre-registration](#pre-registration)) before the first measured run. Until
then, this document makes no empirical claims.

**Design critique welcome:** same invitation as EXP-001 — open an issue if
this methodology has a hole.

---

## Motivation

EXP-001 (results: `exp-001-external-workspace-vs-in-context-triage.md`, final
N=5×3, all conditions complete) measured H2 (cost) at exactly one cadence:
4h/tick, 42 ticks over a simulated week. At that cadence, condition A
(external workspace) used 24 wakes vs. 42 (fixed) for both cron conditions —
a 42.9% reduction, for a small, measured recall cost (0.94 vs 0.98,
precision tied at 1.0).

That result is real but incomplete: it doesn't tell us whether the cost gap
is a fixed percentage or whether it **scales with polling frequency**. The
mechanistic reason to expect scaling: cron's invocation count is coupled to
the tick interval by construction (one call per tick, every tick, whether or
not anything happened). Condition A's invocation count is coupled to
*event salience*, not the polling grid — it should be largely
cadence-independent for a fixed underlying event stream. If that holds, the
absolute call-count gap (not just the percentage) should grow as cron polls
faster, because cron's denominator grows while A's doesn't.

This is also the number EXP-001's design doc asserted from arithmetic, not
measured: "at an actual fast-cron cadence, ~3,360 calls/week vs ~24 wakes."
EXP-002 measures that claim instead of asserting it.

## Hypotheses

Holding the EXP-001 corpus and event timeline fixed, varying only the cron
tick interval for conditions B and C (condition A is cadence-independent by
construction and runs once as the reference):

- **H1 (invocation scaling):** cron invocation count scales ~linearly with
  polling frequency (inversely with tick interval) — halving the tick
  interval roughly doubles B/C's call count. Condition A's invocation count
  stays within measurement noise of its EXP-001 baseline (24±3) across all
  tested cadences.
- **H2 (widening absolute gap):** the absolute call-count gap (B/C wakes
  minus A wakes) grows monotonically as tick interval shrinks. The
  *percentage* gap may or may not grow — H2 is about the absolute number,
  since that's what maps directly to dollars.
- **H3 (detection-latency tradeoff):** faster cron cadence should improve
  cron's time-to-detection (finer-grained polling catches things sooner).
  Prediction: at the fastest tested cadence, cron's time-to-detection
  approaches or beats condition A's — i.e., there IS a real latency cost to
  A's event-driven-not-polling-driven design, and this experiment should
  surface it rather than only reporting the cost win. If A's time-to-
  detection is already at or near cadence-fastest levels (likely, since A is
  event-driven, not polling-bound), state that plainly instead of
  manufacturing a tradeoff that isn't there.

H3 is the one that could go against the architecture, same as H1 did in
EXP-001 — report it as measured, not as hoped.

## Method

### Cadences tested

Same underlying event timeline (EXP-001's amended 224-event corpus,
`scripts/exp001_corpus.jsonl`) replayed at four tick intervals for B/C:

| Label | Tick interval | Ticks/week | Cron calls/week (B, C) |
|---|---|---|---|
| slow | 24h | 7 | 7 |
| baseline | 4h (EXP-001's tested cadence) | 42 | 42 |
| fast | 1h | 168 | 168 |
| very-fast | 15min | 672 | 672 |

Condition A runs once per repeat, independent of the B/C cadence grid — its
wake count is driven by the same underlying event stream regardless of how
finely B/C poll it.

### Conditions

Same three conditions as EXP-001 (A external workspace, B cron+dump, C
cron+ask), same task instruction, same corpus. Only the B/C tick interval
varies across the sweep.

### Metrics

Same as EXP-001 (precision, recall, time-to-detection, invocations, dollar
cost, false-wake rate), reported per cadence for B/C, and once for A as the
cadence-independent reference line on the same chart.

### Repeats

N≥3 per cadence per condition (lower than EXP-001's N=5 — this is a scaling
check across 4 cadence points × 2 conditions = 8 cells, not a single-point
precision estimate; total run count already exceeds EXP-001's).

## Threats to validity (known, invited: find more)

- **Corpus timing resolution.** The underlying event timeline has whatever
  time resolution the original corpus was built at — sub-tick event ordering
  at 15-min cadence may not be meaningfully finer than the corpus's actual
  granularity. Check corpus timestamp resolution before trusting the
  very-fast cell; if events are only timestamped to the hour, the 15-min
  cadence is testing a distinction the corpus can't actually resolve.
- **Condition A's cadence-independence is itself a hypothesis (H1's second
  half), not yet verified across cadences — this experiment tests it, isn't
  entitled to assume it.**
- **Dollar cost extrapolation.** Multiplying measured 42-tick-week costs
  linearly to annual figures compounds any per-call cost estimation error.
  Report weekly figures as measured; be explicit about the extrapolation
  step if annualized numbers are quoted anywhere downstream (e.g. TR-2026-03).

## Pre-registration

Before the first measured run, predicted deltas go in
`exp-002-predictions.md`, committed before any data exists — same discipline
as EXP-001. Results publish either way, including any cell where H1/H2/H3
fail.

## Timeline

Design published 2026-07-11 (same session as the EXP-001 N=5×3 final
readout and the J-space paper mechanism-map work). Harness reuses
`scripts/run_exp001.py` with a new `--tick-interval` parameter — no new
pipeline code needed beyond that, since A/B/C selection and scoring are
already cadence-agnostic. Pre-registration next, before any run.
