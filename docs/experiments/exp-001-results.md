# EXP-001 — Results (Tier 1, N=5, claude-sonnet-5)

**Run:** 2026-07-11, BugaPC. 15 measured runs (5 per condition), ~630 model
calls, harness config `scripts/exp001_claude_config.json` (model pinned to
claude-sonnet-5; all prompts in the repo). Corpus: 224 real background events
+ 10 canaries (see design doc calibration notes 3-4 for the two documented
pre-run amendments). Predictions were committed before any measured run in
[exp-001-predictions.md](exp-001-predictions.md) and are scored below
unchanged — including the ones we got wrong.

Raw per-run JSONL and per-run engine journals: `exp001-tier1/` in this repo.
Sparse-corpus pilot (which caught the density shortfall): `exp001-pilot-clean/`.

## Headline results

| condition | recall mean (range) | precision | false acts | invocations/run |
|---|---|---|---|---|
| A — external workspace (ZugaMind) | **0.94** (0.9–1.0) | 1.00 | 0 | **24** |
| B — cron + dump | **0.98** (0.9–1.0) | 1.00 | 0 | 42 |
| C — cron + ask | **0.98** (0.9–1.0) | 1.00 | 0 | 42 |

Time-to-detection (ticks, mean): A 0.34, B 0.04, C 0.04 (cron is 0-by-
construction at tick granularity; predicted in P5).

## Predictions scored

| # | prediction | verdict |
|---|---|---|
| P1 | A recall ≥ 0.9; B/C ≤ 0.8 | **HALF-FAILED** — A 0.94 ✓, but cron did not degrade (0.98). At ≤11 items/tick, in-context triage is easy. |
| P2 | precision ≥ 0.95 everywhere; won't separate conditions | **HELD** — 1.00 across 630 calls, zero false ACTs. |
| P3 | A tokens ≤ 0.1 × B | **FAILED as written** — measured on prompt characters, A sent ~1.9× more (4KB briefings vs small dumps). See post-hoc note below. |
| P4 | B degrades at >15 items in context | **UNTESTABLE** — max items in any tick was 11. The corpus (even at design spec) never produces the load H3 needs. |
| P5 | A TTD ≤ 1 tick; cron 0 by construction | **HELD** — A mean 0.34. |
| P6 | A false-wake rate 0.3–0.6 | **HELD** — ~50% of A's wakes were ambient-concern wakes (reported, not scored, as pre-registered). |
| P7–P8 | Tier 2 (local qwen3) | **PENDING** — next week. |

## Post-hoc observations (NOT pre-registered — flagged as such)

1. **We pre-registered the wrong cost unit.** P3 measured prompt volume, but
   real billed cost per invocation is dominated by fixed per-call overhead
   (the CLI harness carries a system prompt of roughly 10-15k tokens per
   call, dwarfing both briefings and dumps). By invocation count — the unit
   that actually tracks dollars here — A made 120 calls to cron's 210:
   ~43% fewer. Directionally opposite to the P3-as-written verdict.
2. **The comparison holds cadence fixed at 4 simulated hours, which is
   cron's best case.** Real always-on agents that want minutes-level
   responsiveness must tick fast: a 3-minute cron week is ~3,360 invocations
   against A's ~24 wakes (A's perception ticks are model-free). The
   cheap-vs-responsive tradeoff is the product's actual claim and this
   experiment did not test it. EXP-002 (cadence sweep) will, with its own
   pre-registered predictions.

## Why A missed (3 misses across 5 runs — full traces in the journals)

- **Lottery starvation (2 misses).** Salience selection is weighted-random
  by design. In contention windows a critical alarm bidding 0.86 repeatedly
  lost the draw to hotter modules (including once losing at 1.0 to a 0.79
  bid) until its 3-tick re-emission window closed. An alarm with urgency 1.0
  should not be subject to the lottery — fix: a deterministic alarm lane
  above a criticality threshold (issue filed).
- **Briefing truncation (1 miss).** When the winning module batched multiple
  triggers, the wake briefing carried only the bid's content line (200-char
  cap) — the canary won the competition but its ID never reached the model.
  Detected-by-workspace ≠ delivered-to-model. Fix: the "why you're being
  woken" section must enumerate every trigger in the winning bid (issue
  filed).

Both failure modes were invisible before this experiment. Finding them is
the point of running it.

## Honest summary

On this corpus at this cadence: **detection statistically ties cron
(0.94 vs 0.98 on 10 planted incidents), precision is perfect everywhere,
and the workspace does it with 43% fewer model invocations.** Our 10×
cost prediction failed on the unit we pre-registered; the unit we should
have registered (invocations) favors the architecture, and saying so
belongs in the post-hoc section, not the abstract. The interesting
questions this experiment earned: the cadence sweep (EXP-002) and the two
concrete pipeline fixes above.

## Threats to validity (beyond the design doc's list)

- Corpus skew: ~95% of background events are HN stories (disclosed in
  calibration note 4).
- Corpus-as-run correction: 5 additional real events captured the same
  morning were present in the corpus file during all measured runs but
  missed the freeze commit — the corpus as-run is 229 background events,
  not 224. Committed as-run with this note; canary rows unaffected.
- Single model (claude-sonnet-5) and single harness (claude CLI); Tier 2
  addresses the model axis.
- N=5 with 10 canaries: recall differences of one canary (0.02) are within
  noise; we make no significance claims.
