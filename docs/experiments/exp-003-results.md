# EXP-003 — Results (attention-health ablation, N=5×2, claude-sonnet-5)

**Run:** 2026-07-12/13, BugaPC. 10 measured runs (A×5, D×5), real
`claude -p` harness (model pinned sonnet, `scripts/exp003_claude_config.json`),
wake-decision gate on the local tier (constant-pass in both conditions).
Corpus built fresh per run per the pre-registered design (dominant chatty
source + one buried critical from a different module + one genuine critical
from the dominant source itself), seeds 20260711–15. Predictions committed
before any measured run in [exp-003-predictions.md](exp-003-predictions.md)
and scored below unchanged — all four substantively failed, and the
pre-registered falsifier clause fires. That is the headline.

A first tier-1 attempt (2026-07-11) was invalidated by three operator
errors and is preserved with a full post-mortem in `exp003-tier1-invalid/`.
This valid run also survived a second contamination catch mid-flight: a
co-located deployment's quiet-hours config was silently deferring simulated
wakes (same incident that forced EXP-002's published correction); the runner
now isolates quiet hours and the entire valid dataset postdates that fix.

Raw per-run JSONL and engine journals: `exp003-tier1/`.

## Headline results

| | A — full workspace (health ON) | D — bare lottery (health OFF) |
|---|---|---|
| buried-signal recall (H1, primary) | **5/5 = 1.0** | **5/5 = 1.0** |
| dominant source's own alert (H2) | **1/5 = 0.2** | **5/5 = 1.0** |
| invocations/run (mean) | **10.8** (8–15) | **40.8** (38–42) |
| false ACTs | 0 | 0 |

**The one-sentence result: on this corpus the attention-health layer bought
zero detection advantage — its measured value is doing the same detection at
73% fewer wakes, and pre-fix it actively suppressed the dominant source's
own genuine alerts.**

## Why D matched A on the buried signal

D never dampens the dominant chatter, so the chatter clears the wake floor
almost every tick — D woke ~41 times per simulated week. And the critical
digest (deliberately held constant in both conditions; it is a briefing
mechanism, not a selection mechanism) attaches every active critical to
every briefing. Wake on everything → the digest delivers everything. Brute
force detects by never sleeping. The pre-registered design named this exact
possibility — "a plain max-salience competition might do just as well;
nothing tested so far rules that out" — and it did.

## Why A failed the dominant source's own alert (issue #11)

Traced in the journals to a three-door defect, all doors checking the
*dampened module salience* instead of the *alert's own urgency*: streak
dampening for the module's noise pushed its bids to ~0.15, below the alarm
lane's post-modulation floor (0.5), below the harness wake floor (0.35, the
one time it won selection anyway — A-run0 tick 35), and below the critical
digest's ride-along filter (0.5). The mute button muted the microphone, not
the noise. Fixed same night in c466008 (alarm-fatigue re-keyed on the alarm
class; lane winners bypass the wake floor; digest no longer salience-filters
criticals) — see the verification addendum below. The pre-fix 0.2 stands as
the measured result for the architecture as shipped.

## Predictions scored

| # | prediction | verdict |
|---|---|---|
| P1 | A buried ≥ 0.8, D buried ≤ 0.6, gap ≥ 20pp | **FAILED** — A 1.0 ✓ but D 1.0: no gap at all. The critical digest makes a wake-on-everything strategy detection-complete. |
| P2 | A's dominant-source alert recall not lower than D's | **FAILED, inverted** — D 1.0, A 0.2. The health layer suppressed the very alerts it shared a module with (issue #11). The prediction's own doubt ("dampening is not alert-aware") was the correct half. |
| P3 | if H1 gap exists, ≥ 15pp | **MOOT** — gap is 0pp; treated as "no meaningful effect" per the pre-registered threshold. |
| P4 | invocation counts within ±3 between conditions | **FAILED spectacularly, in A's favor** — A 10.8 vs D 40.8. The design assumed only *which* items win would differ; in fact dampening controls *whether* the floor is cleared at all. The health layer's real measured effect is cost. |

## The falsifier clause, honored

The predictions doc pre-committed: if D matches A's buried-signal recall,
"the attention-health layer is not earning its complexity for this failure
mode... the architecture's main current value is the cost result from
EXP-001, not superior arbitration quality." **That is the conclusion.** The
starvation-resistance story, as a *detection* advantage, is not supported on
this corpus. What survives, consistent with EXP-001 and EXP-002: the same
detection at a fraction of the invocations — A needed 27% of D's wakes for
identical buried-signal recall. The architecture's honest, evidenced claim
after three experiments is **cheap completeness**, not superior perception.

## Post-fix verification addendum (2026-07-13, same night)

Condition A re-run ×5 on the #11 fix (c466008) with IDENTICAL seeds
(20260711–15), raw runs in `exp003-tier1-postfix/`:

| | A pre-fix | A post-fix | D (control) |
|---|---|---|---|
| buried-signal recall | 5/5 = 1.0 | **5/5 = 1.0** | 5/5 = 1.0 |
| dominant source's own alert | 1/5 = 0.2 | **5/5 = 1.0** | 5/5 = 1.0 |
| invocations/run (mean) | 10.8 | **10.8** (9–13) | 40.8 |
| false ACTs | 0 | 0 | 0 |

**Verified: domreal 0.2 → 1.0, buried unchanged, cost unchanged.** With the
three-door defect closed, the full workspace matches the wake-on-everything
control on every detection metric at 27% of its invocations — strict
dominance on this corpus. The conclusion above stands with sharper edges:
the attention layer's evidenced value is cost, and after #11 it no longer
pays a correctness price for it. Timeline for the record: defect measured,
mechanism traced, fix written, tests green, fix verified by re-run — all
within ~4 hours, all in public.

## Threats to validity (beyond the design doc's list)

- **The critical digest is doing enormous, previously unappreciated work.**
  Both conditions' detection rests on it. An ablation of the digest itself
  (A/D × digest on/off) is the obvious EXP-003b — tonight's data cannot
  separate "selection quality" from "bundling coverage" beyond what the
  invocation counts show.
- Synthetic, deliberately adversarial corpus (per the design doc) — the
  BugaPC dogfood deployment's live wake log is the planned real-traffic
  cross-check for how often starvation pressure actually occurs.
- Every planted item carries 0.95 urgency; regimes where the important
  signal *whispers* (sub-alarm urgency) are untested here and are exactly
  where selection quality could still differentiate — candidate EXP-004b.
- N=5 per condition; no significance claims. Detection ties are exact
  (5/5 vs 5/5), so the qualitative conclusion does not rest on statistics.
