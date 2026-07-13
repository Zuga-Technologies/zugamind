# EXP-004b — Whisper regime: does arbitration have a detection niche at all?

**Status:** DESIGNED, not built, not run. Predictions pre-registered in
[exp-004b-predictions.md](exp-004b-predictions.md) before any corpus or
harness code for this experiment exists.

## Motivation

EXP-004(+t) settled the loud-alert regime: per-source tuned gates and the
calibrated workspace are detection-identical and cost-comparable, differing
mainly in configuration burden (S knobs vs 1). But every ground-truth item in
that corpus screamed at 0.95 urgency — above the alarm/override threshold —
so BOTH systems' emergency overrides guaranteed recall by construction, and
selection quality was never actually tested.

This experiment removes the screaming. Planted incidents WHISPER: urgency
0.55–0.85 (below every override), with per-item scores drawn to OVERLAP the
noise distribution, so no single-score threshold can separate incident from
noise cleanly. Gates are structurally limited here: a threshold low enough to
catch whispers floods on noise; high enough to stay quiet, it is deaf. The
workspace's claim to test: multi-signal arbitration (novelty/relevance
patterns, cross-cycle persistence, digest bundling) can find whispers a
one-dimensional line cannot — at some wake cost.

**This is the experiment that can fully retire the architecture's detection
story.** After EXP-003 (detection tie with brute force) and EXP-004
(detection tie with gates), the whisper regime is the last arena where
selection quality could show a measurable advantage. If it ties here too,
the docs retire "smarter detection" permanently and the product's evidenced
claims reduce to the EXP-002/004t cost-and-configuration story.

## Conditions

Same A (full workspace, post-#11 main, calibrated floor per 004t procedure)
and E (steelmanned per-source gates, tuned per the published procedure).
Both overrides remain enabled and both will be inert by construction
(nothing reaches 0.9 urgency) — disclosed rather than removed, so the
conditions are byte-identical to EXP-004t's.

## Corpus — the overlap knob

New builder, extending `build_exp004_corpus.py`'s source plans with:

- Whisper incidents: urgency drawn from [0.55, 0.85], novelty/relevance
  elevated but jittered, all parameters published;
- An OVERLAP parameter sweep: the incident score distribution's separation
  from the noise distribution at d = clean / partial / heavy overlap
  (published as distribution parameters, not adjectives);
- Persistence: whisper incidents re-emit for 3 ticks (a real anomaly
  recurs), matching EXP-001's canary persistence — this is the signal
  arbitration can integrate that a memoryless threshold cannot;
- Grading stays deterministic ACT-line matching on planted ids.

## Metrics

Recall on whisper incidents per overlap level (primary), invocations
(the price A pays to listen harder), false-wake rate, and E's
threshold-placement tradeoff curve (recall vs flood rate at its tuned
threshold, so E's structural ceiling is shown fairly rather than asserted).

## Threats to validity (known, invited: find more)

- Overlap distributions are synthetic constructions; the parameters publish
  so anyone can dispute the realism of the whisper model.
- The persistence assumption (whispers recur) is load-bearing for A's
  integration advantage; a one-shot-whisper variant cell is included so the
  assumption is measured, not smuggled.
- Same author for both conditions; same mitigation as EXP-004 — harness,
  seeds, tuning procedure all publish, stronger baselines welcome as PRs.

## Timeline

Designed 2026-07-13, immediately after EXP-004t closed. Build + runs
scheduled for the coming weekend (2026-07-18/19); results publish either
way, including the retirement scenario.
