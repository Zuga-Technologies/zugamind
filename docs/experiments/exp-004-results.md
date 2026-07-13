# EXP-004 — Results (strong baseline, N=3 per cell, hermetic oracle)

**Run:** 2026-07-13, BugaPC. 24 measured runs (A and E × 2/4/8/12 sources ×
N=3), hermetic oracle instrument, wake-decision gate on the local tier
(constant-pass, disclosed as in EXP-002). Condition A ran on post-#11 main
(c466008) — the architecture as it ships today, including the alarm-lane fix
EXP-003 forced. Predictions were committed before any harness code existed
in [exp-004-predictions.md](exp-004-predictions.md) and are scored below
unchanged. **The headline is a loss: our steelmanned rival ties the
workspace on every detection metric and does it on roughly a third of the
wakes.** Per the pre-registration, that publishes.

A first run set was invalidated by a corpus-builder bug (incident trigger
types were unroutable, so condition A structurally could not perceive any
planted incident) and is preserved with a post-mortem — including how the
smoke's verification grep fooled itself by matching the oracle's own command
string — in `exp004-out-invalid/`. The valid corpus was verified end-to-end
before this run set (incident → router → CRITICAL bid → alarm lane → id in
briefing content).

Raw per-run JSONL, engine journals, calibration data: `exp004-out/`.

## Headline results

| sources | E wakes (runs) | A wakes (runs) | E recall | A recall | H4 newcomer | E params | A params |
|---|---|---|---|---|---|---|---|
| 2 | 2, 3, 3 | 11, 21, 14 | 1.0 | 1.0 | both caught | 2–3 | 0 |
| 4 | 5, 5, 5 | 29, 21, 22 | 1.0 | 1.0 | both caught | 5 | 0 |
| 8 | 8, 9, 9 | 32, 29, 31 | 1.0 | 1.0 | both caught | 9 | 0 |
| 12 | 13, 13, 13 | 42, 39, 38 | 1.0 | 1.0 | both caught | 13 | 0 |

False ACTs: 0 everywhere, both conditions.

**One sentence: per-source tuned gates catch everything this corpus can
throw at them, at every scale, using ~3–6× fewer wakes than the workspace —
the workspace's only standing advantage in this data is zero per-source
configuration (0 knobs vs 2–13).**

A trend worth stating precisely without overclaiming: the wake RATIO narrows
as sources grow (5.8× at S=2 → 3.1× at S=12) because E's wakes grow with
source count while A's one-winner-per-cycle cap bounds its growth — the
direction the arbitration bet predicted, but nowhere near a crossover inside
the tested range. Claims beyond S=12 would be extrapolation; none are made.

## Predictions scored

| # | prediction | verdict |
|---|---|---|
| P1 | detection parity (gap < 10pp) — a pass for gates expected, not a surprise | **HELD** — exact tie, 1.0 everywhere, both conditions, all scales. |
| P2 | A uses ≥30% FEWER invocations than E at ≥4 sources, gap widening | **FAILED, inverted, decisively** — E uses ~70–80% fewer than A at every scale. The asymmetry we missed: E's thresholds were calibrated per source; A ran on its factory-default wake floor (0.35), tuned to nothing, so it woke for ambient chatter E's tuned gates ignored. Addendum EXP-004t (pre-registered cdbb155, before any harness code) closes the symmetry with ONE calibrated global floor for A. |
| P3 | at ≤2 sources E matches A within 10% (simplicity wins small) | **FAILED in E's favor** — E didn't match A, it beat A ~5.8× even at 2 sources. |
| P4 | untuned mid-window newcomer: A catches with zero config; E misses or floods | **HALF-FAILED** — A caught it with zero config as predicted, but E also caught it cleanly: its urgency override (granted by the steelman spec as alarm-lane parity) never even strained. With ground truth that always screams at 0.95 urgency, the override alone guarantees E's recall — see threats. |

## The falsifier clause, partially honored

The pre-registration committed: if E ties detection AND matches cost AND
handles the untuned source, "shared arbitration is decoration at every scale
tested." E did better than match cost — it won. What keeps the clause from
closing entirely: (1) the config-parameter asymmetry is real and grows
linearly with sources (13 tuned, drift-prone thresholds at S=12 vs 0), and
(2) the tuning asymmetry in P2's scoring is a measured methodological gap
with a pre-registered addendum in flight (EXP-004t), not an excuse. If
EXP-004t's calibrated-floor workspace reaches wake parity, the honest
summary becomes "same detection, same cost, 1 knob vs 13." If it doesn't,
gates win this corpus outright and the doc will say so.

## Threats to validity (known, invited: find more)

- **This corpus is the gates' best case, by design.** Stationary noise
  profiles, cleanly separable incident scores, and every ground-truth item
  at 0.95 urgency — which E's granted urgency override auto-catches, making
  its recall partially guaranteed by construction. The regimes gates
  structurally can't serve — drifting noise (thresholds rot), whispering
  signals (below any tuned line), cross-source correlation — are exactly
  what EXP-004b will test, pre-registered separately. A steelman deserves a
  favorable field; conclusions must not travel beyond it.
- **We built the rival.** Mitigation: the tuning procedure, calibration
  corpus, seeds, and harness are all published — re-tune E and re-run; a
  stronger E than ours is a welcome PR.
- N=3 per cell; detection ties are exact counts, cost gaps are ~3–6× (far
  beyond run-to-run variance of ±3 wakes); no significance claims.

## EXP-004t addendum (same day) — one calibrated knob reaches parity

Condition At = the identical workspace with its single global wake floor
calibrated by the same published procedure E's thresholds received
(predictions pre-registered in
[exp-004t-predictions.md](exp-004t-predictions.md) before the calibration
harness existed). Same corpora, same seeds, N=3 per cell:

| sources | E wakes (mean, S params) | At wakes (mean, 1 param) | ratio |
|---|---|---|---|
| 2 | 2.7 (2–3 knobs) | 4.0 | 1.48× |
| 4 | 5.0 (5 knobs) | 5.7 | 1.13× |
| 8 | 8.7 (9 knobs) | 10.0 | 1.15× |
| 12 | 13.0 (13 knobs) | **13.3** | **1.03×** |

Recall 1.0 everywhere, H4 newcomer caught everywhere, false ACTs 0 — the
raised floor cost no detection, because post-#11 alarm-lane winners bypass
the floor by design (this addendum is only safe BECAUSE of EXP-003's fix;
the experiments compound).

All four pre-registered predictions held: P1 (no detection cost), P2
(within 1.5× of E everywhere — 1.48× at S=2 sits just inside the band and
is reported as the near-miss it is), P3 (At never *beats* E — tuned
per-source gates remain the theoretical cost floor on this corpus), P4
(1 parameter vs S parameters, by construction).

Note the direction of the ratio: parity IMPROVES with scale (1.48× → 1.03×).
One self-calibrated knob buys near-exact cost parity with S hand-tuned
thresholds precisely where maintaining S thresholds hurts most.

**Revised honest summary for the whole experiment:** on the gates' best-case
corpus, tuned per-source gates and the calibrated workspace are
detection-identical and cost-identical at scale; the difference that
remains is operational — 1 knob vs S knobs — plus the untested regimes
(drift, whispers, cross-source correlation) reserved for EXP-004b. The
as-measured loss above stands for the untuned default, and shipping a
better default floor derived from this calibration procedure is now an
open product question (issue to follow), not an experiment question.
