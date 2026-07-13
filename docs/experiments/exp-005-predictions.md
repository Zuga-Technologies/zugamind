# EXP-005 — Pre-registered predictions

**Committed 2026-07-13, at the opening of the observation window. No wake
inside the window has occurred yet; the 3 pre-window wakes (2 worth-it, 1
noise, including the wake that led to the #6 fix) are motivation, not data.**
Design: [exp-005-value-of-wakes.md](exp-005-value-of-wakes.md).

| # | Prediction | Confidence |
|---|---|---|
| P1 | Outcome value rate >= 0.5 — at least half of the window's wakes lead to a hard or recorded outcome within 48h (criteria a/b/c) | 0.5 |
| P2 | Self-grade calibration >= 0.7 — the woken instance's own worth-it/noise verdict agrees with the outcome grade at least 70% of the time. The system knowing when it wasted a wake is worth more than a high hit rate | 0.55 |
| P3 | Noise rate <= 1/3 of wakes (by outcome grade, not self-grade) | 0.6 |
| P4 | At least one wake in the window leads to a HARD outcome (a merged commit/PR or a closed issue, criteria a or b — not just an operator decision) | 0.7 |
| P5 | Wake volume stays inside the deployment's caps and under 6/day mean — the layer's cost discipline holds on real traffic, not just synthetic weeks | 0.75 |

## What would falsify the core bet

If the outcome value rate lands under 0.25 — three of four wakes producing
nothing within 48h — then on this deployment the honest conclusion is that
the perception layer's cheap completeness is NOT yet translating into work,
and the product story must lead with monitoring value, not work value, until
a redesigned wake-to-action path earns better numbers. Publishes either way.
