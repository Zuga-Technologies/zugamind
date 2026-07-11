# EXP-003 — Attention-health ablation: does self-regulation earn its keep?

**Status:** DESIGNED, not yet run. Predictions pre-registered before any
measured run, same discipline as EXP-001/002.

---

## Motivation

EXP-001 measured ZugaMind against cron baselines (no gate at all) and found
a real cost win (43% fewer calls). Its first measured run also showed a
recall gap, root-caused to a real starvation bug in `_select_winner` and
fixed same-day (`exp-001-acceptance.md`) — post-fix, recall is now equal to
cron. That fix added two new mechanisms this design must now account for.
**Selection now has three stacked layers, not one:**

| Layer | What it does | Tests as "attention health" in this experiment? |
|---|---|---|
| 2. `AttentionSchema.modulate()` + diversity cap | Soft corrections — dampens repeat winners, caps over-represented sources, boosts neglected ones | YES — original EXP-003 target |
| 3. Alarm lane (`_select_winner`, added post-EXP-001) | Hard override — critical-urgency bids skip the lottery, rotate fairly among themselves | YES — added to the ablation scope, since it's a selection/arbitration mechanism like layer 2 |
| 4. Critical digest (briefing/journal construction) | Bundles every active critical alert into whichever briefing goes out — not a selection mechanism, decides what rides along *after* a winner is already chosen | **NO — held constant in both conditions.** This was the fix that actually closed EXP-001's recall gap (not the alarm lane alone — see acceptance doc's staged numbers: alarm lane alone only reached 0.92, still below cron; adding the digest reached 0.98). Letting it vary between A and D would confound "does selection fairness matter" with "does bundling matter," a different question this experiment isn't built to answer. |

A plain max-salience competition, with none of the selection-layer
corrections (layers 2+3) active, might do just as well as the full system —
nothing tested so far rules that out.

This gap surfaced directly from a landscape check (2026-07-11): published
cost-optimization techniques for LLM agents report better raw cost numbers
than EXP-001's 43% (up to 87%+ reduction via simple deterministic gating).
If ZugaMind's only claim were "cheaper than cron," it would already be
beaten by simpler approaches. The architecture's actual candidate advantage
— IF it exists — is handling a harder failure mode plain threshold/max
gating doesn't: **starvation**, where one persistently moderate-salience
source keeps winning and a real, rarer signal from elsewhere never
surfaces. EXP-003 tests whether that advantage is real or assumed.

## Hypothesis

Under a corpus engineered to create starvation pressure (see Corpus below),
holding bid generation and one-winner-per-cycle selection constant:

- **H1 (starvation resistance):** condition A (full `AttentionSchema`,
  streak dampening + diversity cap + blind-spot boost active) achieves
  higher recall on the *buried* signal than condition D (bare max-salience
  selection, health corrections disabled) — the buried signal is more
  likely to win at least once under A than under D.
- **H2 (no free lunch check):** condition D's recall on the *dominant*
  source's own legitimate alerts (if any are planted) is not worse than
  A's — i.e., we're checking that A's protection for the buried signal
  doesn't come at the cost of missing real alerts from the dominant source
  when they matter. If A trades one for the other, that's a real tradeoff
  to report, not a clean win.
- **H3 (magnitude, not just direction):** if H1 holds, the recall gap is
  large enough to matter practically (pre-registered threshold: ≥15
  percentage points), not a marginal difference indistinguishable from
  run-to-run noise at N=5.

## Method

### Conditions

| | Condition | Difference from condition A |
|---|---|---|
| A | Full workspace (post-EXP-001-fix condition A: soft modulation + alarm lane + critical digest all active) | — |
| D | Bare competition | `AttentionSchema.modulate()`, `apply_hard_diversity_cap()`, AND the alarm lane all bypassed — winner is the raw max-salience bid via the unmodified weighted lottery every cycle. Critical digest STAYS ACTIVE in both A and D (it's not part of what's being tested). Everything else (bid generation, broadcast, logging) identical to A. |

Implementation: a single flag on `Workspace`, `attention_health_enabled:
bool = True`, that short-circuits the soft-modulation calls in `run_cycle()`
AND the alarm-lane branch in `_select_winner()` when False, falling straight
through to the weighted lottery. Does not touch bid generation, broadcast,
or the critical-digest briefing path — any measured difference is
attributable to the selection-layer corrections alone.

### Corpus — new, not reused from EXP-001

EXP-001's corpus was explicitly diversified across four modules *to avoid*
triggering the diversity cap (calibration note 3 in the EXP-001 design
doc) — the opposite of what EXP-003 needs. New corpus requirements:

- One **dominant source**: a single (module, target) identity that emits a
  moderate-but-above-wake-floor salience bid on most ticks across the
  simulated window (e.g., a chatty but not urgent monitoring feed).
- One **buried signal**: a single genuinely important planted event from a
  *different* (module, target) identity, injected once, competing against
  the dominant source's ongoing bids at the moment it appears.
- One **dominant-source-real-alert** (for H2): a planted event that IS a
  legitimate alert from the dominant source itself, to check A doesn't
  over-suppress its own frequent bidder into missing real news.

### Metrics

Recall on the buried signal (primary, H1), recall on the dominant-source
real alert (H2), invocation count (secondary — expect similar between A
and D since total event volume is unchanged, only *which* items win
differs).

### Repeats

N≥5 per condition, varying which tick the buried signal and the dominant
real-alert land on, same as EXP-001/002's repeat discipline.

## Threats to validity (known, invited: find more)

- **Corpus is synthetic-by-construction** (a deliberately adversarial
  dominant source), unlike EXP-001's real-Hacker-News-backed background —
  this is a stress test, not a naturalistic sample. State that plainly in
  any writeup; don't let "starvation resistance confirmed" imply it happens
  often in real traffic without separately checking real-world dominance
  patterns.
- **Single dominant source tested.** Real starvation could come from
  multiple moderately-loud sources interacting, not just one — this design
  tests the simplest case first.
- **Bare competition (D) is a strawman for "no health layer," not
  necessarily representative of a well-built ad-hoc classifier gate** —
  the broader "does GWT structure beat a strong independent alternative"
  question (per-source gates vs. one shared arbitration) is explicitly out
  of scope here; that's a separate future experiment (candidate EXP-004),
  kept separate so this result isn't confounded across two different
  mechanisms.

## Pre-registration

Predicted deltas for H1/H2/H3, with the ≥15pp threshold for H3, go in
`exp-003-predictions.md` before any measured run.

## Timeline

Design published 2026-07-11, same session as EXP-002; revised same day
after EXP-001's post-fix acceptance run surfaced the alarm lane + critical
digest as new mechanisms requiring scope decisions (alarm lane in-scope for
ablation, critical digest explicitly held constant). Needs: (1) the
`attention_health_enabled` flag on `Workspace` (small code change,
condition D) — building now, (2) a new corpus-builder script for the
dominant/buried pattern (cannot reuse `build_exp001_corpus.py` as-is) —
not yet built.
