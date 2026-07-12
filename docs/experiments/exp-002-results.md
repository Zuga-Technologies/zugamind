# EXP-002 — Results (cadence sweep, N=3 per cell, hermetic oracle)

**Run:** 2026-07-12, BugaPC. 36 measured runs (3 conditions × 4 cadences × N=3),
zero failed cells. Instrument: the hermetic oracle harness (deterministic
briefing-reader) with the wake-decision gate on the local tier — NOT the
claude-sonnet-5 CLI used for EXP-001 tier-1. That choice is deliberate and
disclosed: every EXP-002 hypothesis is about *mechanical scaling* (invocation
counts, detection latency), not model reading ability, and the oracle isolates
exactly that. Recall figures below therefore mean "the pipeline delivered the
incident to a briefing a perfect reader would catch" — the model-reading step
was measured separately in EXP-001 at the 4h cadence (0.98 with sonnet).

Corpus: EXP-001's frozen as-run corpus (229 background events + 10 canaries),
re-verified before the sweep — a live capture task was found still appending
to the file and was disabled; the corpus was restored to the committed as-run
state (239 rows) before any run. Harness: `run_exp001.py` with timeline
re-bucketing (`--tick-hours`); the 4h default reproduces EXP-001 byte-for-byte
(smoke: A=24 / B=42 / C=42). Predictions were committed before any measured
run in [exp-002-predictions.md](exp-002-predictions.md) and are scored below
unchanged — including the one we got wrong on both edges.

Raw per-run JSONL, engine journals, and per-cadence summaries: `exp002-sweep/`.

## Headline results

| cadence (tick) | ticks/week | A wakes (runs) | B wakes | C wakes | absolute gap (B−A, mean) |
|---|---|---|---|---|---|
| slow (24h) | 7 | 7, 7, 7 † | 7 | 7 | 0 |
| baseline (4h) | 42 | 24, 24, 22 | 42 | 42 | 18.7 |
| fast (1h) | 168 | 23, 26, 21 | 168 | 168 | 144.7 |
| very-fast (15min) | 672 | 16, 22, 17 | 672 | 672 | **653.7** |

† Grid-clamped: A cannot wake more than once per tick, so at 7 ticks its
ceiling is 7. The clamp binds only when the grid is coarser than the event
stream — see P2 scoring.

Recall: 1.0 in 35 of 36 runs (one slow/A run at 0.9 — see post-hoc note 3).
Precision 1.0 everywhere. Cron TTD: 0 ticks in every cell by construction;
A TTD (hours, mean): 0.93 at baseline, 0.27 at fast, 1.47 at very-fast,
max observed 12h.

**The one-sentence result: cron's cost is a straight line through the polling
rate (7 → 42 → 168 → 672 calls/week, exactly), ZugaMind's is flat (~16–26
calls/week at every cadence at or below 4h), and at 15-minute polling the gap
is ~654 model calls per week — 35× the gap measured at EXP-001's cadence —
for zero measurable detection benefit on this corpus.**

## Predictions scored

| # | prediction | verdict |
|---|---|---|
| P1 | cron invocations == tick count exactly at every cadence | **HELD** — 7/42/168/672 exactly, all runs, both conditions. |
| P2 | A stays within 18–30 wakes across all four cadences, no monotonic trend | **HALF-FAILED as written** — the *claim under test* (cadence-independence) held emphatically: A never scales with the grid. But the pre-registered band was wrong on both edges: slow runs sit at 7 (grid-clamped — the band ignored the tick ceiling) and two very-fast runs came in at 16–17, *below* the band. No monotonic increase; if anything a mild decrease at very-fast (post-hoc note 2). |
| P3 | absolute gap at very-fast ≥ 180 (≥10× baseline's 18) | **HELD** — 653.7, i.e. 35× baseline. The prediction was conservative by 3.5×. |
| P4 | at 15min cadence cron's TTD beats A's baseline TTD — the prediction most likely to go against the architecture | **HELD (against us, as designed)** — cron detects at tick 0 in every cell; A pays a real latency cost (mean ~0.3–1.5h, max 12h). The event-driven design is cheaper, not faster. Granularity caveat: TTD is measured at tick resolution, so sub-tick latency is invisible for both conditions. |
| P5 | diminishing returns at very-fast vs fast (corpus resolution ceiling) | **UNTESTABLE — floor effect** — cron's TTD was already 0 at *every* cadence including 24h, so there was no headroom in which returns could diminish. The corpus-resolution concern was real but showed up more starkly than predicted: on this corpus, polling 16× faster bought cron no measurable detection improvement at all. |

## Post-hoc observations (NOT pre-registered — flagged as such)

1. **Dollar framing without a price assumption.** The measured quantity is
   calls/week. At any per-call cost p, the weekly saving at 15-min
   responsiveness is ~654p and the annualized saving ~34,000p. EXP-001's
   post-hoc note applies unchanged: per-call cost is dominated by the fixed
   CLI overhead (~10–15k system-prompt tokens), so p is not small.
2. **A's mild wake-count dip at very-fast (16–22 vs 21–26 at fast) was not
   predicted and is not yet explained.** Candidates: more empty perception
   cycles changing the soft-modulation history, or salience-floor
   interactions when native events spread across a finer grid. It moves in
   the architecture's favor, which is exactly why we flag it rather than
   celebrate it — an unexplained favorable result is a threat to validity
   until it's mechanistically understood. Open item for EXP-002b or a code
   walkthrough.
3. **The single 0.9-recall run (slow/A) is starvation-shaped.** At 24h ticks,
   six native ticks of events compete in each cycle and one canary never won
   a wake or a digest slot. One run at N=3 proves nothing, but it rhymes with
   EXP-003's thesis: contention pressure, not polling rate, is where
   detection risk lives for the workspace design.

## Honest summary

The scaling claim from EXP-001's design doc is now measured instead of
asserted: **cron's invocation count is the polling rate; ZugaMind's is the
event stream.** At minutes-level responsiveness the weekly call gap is ~654
(35× the 4h-cadence gap), recall is flat at 1.0 for both architectures under
a perfect reader, and the honest cost of the event-driven design is latency —
detection can lag onset by up to 12 simulated hours where cron at any
cadence detects within its tick. Cheap and complete, not instant: pick the
architecture by which failure mode your deployment can afford.

## Threats to validity (beyond the design doc's list)

- Oracle instrument: model reading ability held perfect by construction;
  combine with EXP-001's tier-1 numbers for the end-to-end picture at 4h.
  A tier-1 replication of one sweep cell is cheap future work.
- Corpus timestamps resolve to the 4h native grid; the very-fast cell tests
  polling economics, not sub-4h event dynamics (this drove P5's floor).
- N=3 per cell, pre-registered as a scaling check, not a precision estimate;
  no significance claims made or implied.
- The wake-decision gate ran on the local tier (qwen2.5:7b) rather than
  EXP-001's default; the gate refused zero wakes in either configuration, so
  it is a constant-pass step in both, but the substitution is disclosed.
