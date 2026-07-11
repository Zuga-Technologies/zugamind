# EXP-001 — Pre-registered predictions

**Committed 2026-07-11, before the first measured (non-oracle) run.**
Immutable from this commit forward: results publish either way, including
the ones that prove these numbers wrong. Design and method:
[exp-001-external-workspace-vs-in-context-triage.md](exp-001-external-workspace-vs-in-context-triage.md).

Context available when these were written: the hermetic oracle smoke only
(deterministic canary-echo harness, zero model calls). Oracle results —
A: recall 1.0 / precision 1.0 / 22 wakes; B and C: recall 1.0 / precision
1.0 / 42 wakes each. The oracle measures the pipeline, not the model: it
proves every canary reaches the harness in all three conditions. Every
number below is a prediction about what a real model does with that input.

## Tier 1 (claude, N=5 per condition)

| # | Hypothesis | Prediction | Confidence |
|---|---|---|---|
| P1 | H1 recall | Condition A recall ≥ 0.9 (mean over runs); B and C recall ≤ 0.8, because canaries embedded in 20+ item dumps get skipped | 0.55 |
| P2 | H1 precision | A precision ≥ 0.95; B precision ≥ A − 0.05 (the explicit ACT-id format makes false positives rare in ALL conditions — precision alone will NOT separate the conditions) | 0.6 |
| P3 | H2 cost | Total tokens billed: A ≤ 0.1 × B (order of magnitude). Mechanically near-certain (22 small briefings vs 42 accumulating dumps); we predict the measured ratio lands between 0.02 and 0.08 | 0.7 |
| P4 | H3 degradation | In B, per-canary detection rate for canaries arriving in ticks with > 15 accumulated competing items is at least 10 points lower than for ticks with ≤ 5 | 0.5 |
| P5 | Time-to-detection | A mean TTD ≤ 1 tick (canaries re-emit 3 ticks and win fast); B/C TTD = 0 by construction (every tick dumps everything) — TTD will favor cron, and we say so up front | 0.6 |
| P6 | Wake economics | A false-wake rate (wakes on non-canary winners / total wakes) between 0.3 and 0.6 — the workspace surfaces real-but-unplanted items (that is the product working, not an error; reported, not scored) | 0.5 |

## Tier 2 (local qwen3 via Ollama, N=5 per condition)

| # | Prediction | Confidence |
|---|---|---|
| P7 | The A-vs-B recall gap WIDENS on the smaller model (weaker long-context triage makes external selection matter more): gap(qwen3) ≥ gap(claude) + 5 points | 0.5 |
| P8 | Direction of every Tier-1 result (P1–P4 signs) replicates on Tier 2 | 0.6 |

## What would falsify the core bet

If B or C matches A on recall at ≤ 2× A's token cost, the external
workspace is not earning its complexity for this corpus size, and we will
say exactly that in the writeup.
