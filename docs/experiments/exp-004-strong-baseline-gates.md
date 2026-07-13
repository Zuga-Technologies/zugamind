# EXP-004 — Strong baseline: shared arbitration vs per-source threshold gates

**Status:** DESIGNED, not yet run. No harness code exists. Predictions
pre-registered in [exp-004-predictions.md](exp-004-predictions.md) before any
implementation, same discipline as EXP-001/002/003.

---

## Motivation

EXP-003's own threats-to-validity section says it plainly: condition D (bare
max-salience lottery) is a strawman for "no attention layer." The credible
alternative a competent engineer would actually build is **per-source
threshold gates** — one independent salience/urgency threshold per feed, no
shared anything. Published cost-optimization results for LLM agents using
simple deterministic gating report up to 87%+ call reduction, better than
EXP-001's measured 43%. If per-source gates also match the workspace on
detection, the architecture's arbitration layer is decoration.

This experiment is deliberately dangerous to the architecture. One honest
mechanistic concession up front: **starvation — the failure mode EXP-003
tests — is partly an artifact of the workspace's own one-winner-per-cycle
design.** Independent gates have no shared winner slot, so a chatty source
cannot starve a quiet one by construction. On pure detection over a
starvation corpus, a well-built gate baseline plausibly ties or beats the
workspace. What gates structurally lack is different: a global invocation
budget (N noisy sources = N independent wake streams), cross-source
prioritization (no shared view, no ordered briefing), and zero-tuning
onboarding (every threshold is a per-source magic number someone maintains).
Whether those structural advantages show up as measurable wins — and at what
source count they start paying — is what this experiment measures.

## Conditions

| | Condition | Description |
|---|---|---|
| A | Full workspace | Post-EXP-001-fix pipeline: soft modulation + alarm lane + critical digest, one wake per cycle max, digest carries losing criticals. |
| E | Per-source gates (STEELMANNED) | One threshold per source; an item above its source's threshold triggers a wake. Steelman requirements below are binding. |

### Steelman requirements for condition E (binding)

A reviewer must not be able to build a stronger simple baseline than we did.
Condition E gets:

1. **Digest-equivalent batching:** all above-threshold items in the same tick
   share ONE wake with a combined briefing (per-tick coalescing), so E is not
   penalized wake-per-item.
2. **Fair tuning budget:** thresholds tuned on the same calibration data the
   workspace's constants were tuned on, with the tuning procedure published.
3. **Urgency override:** critical-urgency items (>= the alarm-lane threshold)
   always fire regardless of threshold — the same guarantee A's alarm lane
   provides.

If E loses, it loses at its best. If A loses to our own steelman, we publish
that and the arbitration claim is retired (see falsifier in predictions doc).

## Hypotheses

Across corpora with varying source counts (2 / 4 / 8 / 12 sources, mixed
chatty + quiet, planted ground-truth incidents per EXP-003's canary scheme):

- **H1 (detection parity):** E ties A on recall of planted incidents,
  including EXP-003's buried-signal trap — gates have no winner-slot
  bottleneck, so starvation does not transfer to them.
- **H2 (cost scaling with source count):** A's invocation count grows slower
  than E's as source count rises; pre-registered threshold: A uses >=30%
  fewer invocations than E at >=4 sources.
- **H3 (small-scale parity):** at <=2 sources, E matches A's invocation count
  within 10% — i.e., below ~3 sources the workspace's coordination buys
  nothing measurable and simplicity wins.
- **H4 (untuned-source robustness):** when a NEW source is added mid-window
  with no configuration change, A still catches that source's planted
  incident (workspace needs no per-source config); E with its documented
  default threshold either misses it or floods (whichever its default does —
  both are failure modes a real deployment would hit).

H1 and H3 are expected LOSSES or ties for the architecture, pre-registered as
such. The bet worth making is H2+H4: shared arbitration pays as sources
scale and as configuration drifts, not in a two-feed toy.

## Metrics

Recall/precision on planted incidents (per EXP-001 deterministic ACT-line
grading), total invocations per condition per source count, count of
per-source configuration parameters each condition requires ("magic
numbers"), and the H4 outcome (caught / missed / flooded).

## Threats to validity (known, invited: find more)

- **We built the baseline.** Steelman requirements above are the mitigation;
  the tuning procedure publishes so anyone can retune E and re-run.
- **Synthetic corpora again** (same limitation as EXP-003). The BugaPC
  dogfood deployment's live wake log is the planned real-traffic
  cross-check for the source-dominance assumptions used here.
- **Source-count grid tops out at 12.** Claims beyond that are extrapolation
  and must be labeled as such.

## Timeline

Designed 2026-07-12 (same session as the EXP-002/003 execution runs), before
any EXP-004 harness code. Predictions committed with this design, before any
measured run. Runs after EXP-002/003 results publish.
