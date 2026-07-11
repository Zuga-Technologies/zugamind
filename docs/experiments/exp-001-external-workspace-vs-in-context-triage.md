# EXP-001 — External salience selection vs. in-context triage

**Status:** DESIGNED, not yet run. Predictions will be pre-registered (see
[Pre-registration](#pre-registration)) before the first measured run. Until
then, this document makes no empirical claims.

**Design critique welcome:** if this methodology has a hole, we want to hear
it *before* we run, not after — that's the point of publishing the design
first. Open an issue.

---

## Motivation

ZugaMind's core bet is that *selection should happen before the model call*:
deterministic scanners feed a Global Workspace–style salience competition,
and the agent is only invoked — and only billed — after something wins and
clears a budget gate.

The alternative most people actually run is in-context triage: wake the agent
on a schedule (cron, heartbeat) and let the model itself decide what in the
accumulated input matters.

Anthropic's global-workspace interpretability findings (July 2026) showed a
workspace-like bottleneck *emerging inside* Claude. ZugaMind is the same
functional architecture *engineered outside* the model. Those are different
things and this experiment does not connect them mechanically — no one
outside Anthropic can observe the internal workspace. What we can measure is
behavioral: **does putting a workspace in front of the model beat asking the
model to be its own workspace?** If the architecture matters, it should
matter measurably, in precision, latency, and dollars.

## Hypotheses

For a fixed event corpus with planted ground-truth-important items:

- **H1 (detection quality):** the external-workspace condition achieves
  higher precision at equal-or-higher recall on planted items than either
  cron condition — fewer wasted wakes, no missed canaries.
- **H2 (cost):** total tokens billed by the external-workspace condition are
  at least an order of magnitude lower than cron+dump, because idle
  perception is free and only winners are escalated.
- **H3 (context-load degradation):** in the cron+dump condition, detection
  quality degrades as the number of accumulated competing items per call
  grows; the external-workspace condition is flat by construction (the model
  always sees one winner plus continuity).

Directional predictions with numbers attached will be pre-registered before
the run (see below). H3 is the interesting one: it tests *why* external
selection helps — long-context interference — not just *that* it helps.

## Method

### Corpus

- ~200 scanner events replayed over a simulated multi-day window, drawn from
  real scanner output (hackernews / RSS / GitHub scanners) so the
  distribution is honest, plus **K planted canary items** (K≈10) with
  unambiguous ground-truth importance, following the existing
  `verify_harness.py` canary pattern.
- Canary placement is randomized across the window per run. The corpus is
  frozen and shipped in the repo so anyone can replay it.

### Conditions

| | condition | what the model sees |
|---|---|---|
| A | **external workspace** (ZugaMind) | one briefing per wake: the competition winner + continuity context |
| B | **cron + dump** | every tick, all events accumulated since the last tick |
| C | **cron + ask** | every tick, same accumulation, prompted "does anything here need action?" |

Same corpus, same underlying model, same task instruction ("act on items
that meet <criteria>; ignore the rest") in all conditions.

### Harnesses

- Tier 1: `claude` (paid path), via the shipped harness config.
- Tier 2: local model via Ollama (qwen3), the $0 replication anyone can run
  without keys. Tier 2 runs after Tier 1, same corpus, same conditions.

### Metrics

- Precision / recall on the K planted items (did the agent act on canaries,
  did it act on non-canaries).
- Time-to-detection per canary (simulated-clock delta from event to action).
- Total tokens billed and dollar cost per condition.
- Wake count and false-wake rate.
- For H3: detection rate binned by number of competing items in context.

### Repeats and nondeterminism

Each condition runs N≥5 times with different canary placements. Models are
nondeterministic; we report per-run raw results, not just aggregates.

## Pre-registration

Before the first measured run, the predicted deltas (with confidence levels)
will be committed to this repo in `docs/experiments/exp-001-predictions.md`
— written down, dated, and immutable before any data exists. Results get
published either way, including the runs where we were wrong. The raw
per-run JSONL ships alongside the writeup.

## Threats to validity (known, invited: find more)

- **Canary realism:** planted items are only a proxy for "important." We
  mitigate by deriving canaries from real historical events (e.g., a genuine
  dead-feed alarm) rather than synthetic strings, but the proxy gap is real.
- **Prompt asymmetry:** conditions B/C necessarily use a different prompt
  shape than A. We publish all prompts verbatim; critique welcome.
- **Cost accounting asymmetry:** A pays a small fixed cost in local compute
  for scanning/competition; we report it, though it rounds to $0 in API
  terms.
- **Author bias:** we built the thing being tested. That's exactly what
  pre-registration, frozen corpus, published raw runs, and a replicable $0
  local tier are for. Run it yourself: the harness will ship as
  `scripts/run_exp001.py`.

## Tier 3 — the experiment we can't run (an open invitation)

Whether a pre-triaged briefing produces measurably different *internal*
workspace activity than a raw feed dump — less in-context competition among
irrelevant items, in the terms of Anthropic's global-workspace findings — is
an interpretability question requiring activation access. We can't run it;
Anthropic's interpretability team, or academics working on open-weights
workspace analyses, could. If that's you and the behavioral results (H3
especially) look interesting, we'd genuinely like to help set it up: the
corpus, conditions, and harness here are designed to be reusable as the
behavioral half of that study.

## Calibration notes from the hermetic smoke run (2026-07-10, pre-data)

The harness (`scripts/run_exp001.py`) and a deterministic oracle harness (a
subprocess that echoes `ACT:` lines for canary IDs in its input — full
pipeline, zero model) surfaced three design decisions before any real run:

1. **Canary persistence.** A one-shot canary can lose its tick's
   weighted-random competition and vanish — unrealistic, since a genuine
   monitored-source failure re-alarms every scan until fixed. Canaries now
   re-emit for 3 consecutive ticks; time-to-detection counts from onset.
2. **Wake floor pre-declared.** Condition A's harness config sets
   `wake_min_salience: 0.35` — the product's own selectivity lever. Without
   it, every cycle winner dispatches and condition A's wake count is
   meaningless.
3. **Calibration question — SETTLED 2026-07-11 (canary diversification):**
   ten same-type canaries in one simulated week trip the attention schema's
   diversity dampening — later canaries get suppressed below the wake floor
   *by real product behavior* (repeated same-class alarms habituate). The
   oracle smoke measured condition A at recall 0.6 against the homogeneous
   corpus — suppression, not detection failure. We chose **diversification**:
   the ten canaries now span four workspace modules (infrastructure ×3,
   daemon ×3, schedule ×2, repo_issues ×2), matching how a real multi-day
   incident window spans failure classes. Incident texts and the `detail`
   field are byte-identical to before, so conditions B/C (which render only
   `detail`) are unaffected — the change alters only condition A's routing
   surface. Post-change oracle smoke: A recall 1.0, precision 1.0, 22 wakes
   (vs 42 ticks in B/C). No pipeline code changed in response to scores; the
   change is `build_exp001_corpus.py --rebuild-canaries` and is in the git
   history.

4. **Corpus-density amendment — 2026-07-11, after the Tier-1 pilot, before
   any measured run.** The pilot (1 run per condition, real model, results
   in the repo history) revealed the captured background set had only 25
   events against the design's ~200-event spec — cron ticks averaged under
   2 items, so H3's >15-item bins could not occur and condition B ran
   against a near-empty week (its best case). Amendment: backfilled the
   background set to 224 events using REAL Hacker News stories from the
   preceding 7 days via the Algolia API (each row carries its verifiable
   story_id and url; shape identical to the hackernews scanner's output;
   nothing synthetic). Known skew: the backfilled corpus is 214/224
   hackernews_story — acceptable for the background's role (competing
   noise) and disclosed here. Canary rows are byte-identical to the
   pre-registration commit; the predictions in exp-001-predictions.md are
   unchanged and remain binding. Pilot results from the sparse corpus
   publish alongside the measured runs.

## Timeline

Design published 2026-07-10. Harness + hermetic tests + oracle smoke landed
same day. Predictions pre-registered 2026-07-11; Tier-1 pilot same day caught
the corpus-density shortfall (calibration note 4); measured N=5 runs follow
on the amended corpus. Tier 2 (local replication) the following week.
