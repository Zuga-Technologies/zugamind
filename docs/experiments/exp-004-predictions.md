# EXP-004 — Pre-registered predictions

**Committed 2026-07-12, before any harness code or measured run.** Immutable
from this commit forward; results publish either way. Design and steelman
requirements: [exp-004-strong-baseline-gates.md](exp-004-strong-baseline-gates.md).

Context available when these were written: EXP-001 final (A recall 0.94 /
24 invocations vs cron 0.98 / 42), EXP-002 and EXP-003 runs IN FLIGHT (no
results known yet). These predictions deliberately include expected losses —
the point of a strong baseline is that it can win.

| # | Hypothesis | Prediction | Confidence |
|---|---|---|---|
| P1 | H1 — detection parity | Condition E (steelmanned per-source gates) ties condition A on buried-signal recall (both >= 0.8; gap < 10pp). Starvation does NOT transfer to gates — it is a winner-slot artifact, and gates have no winner slot. A "pass" for the architecture here would be a surprise, not the expectation | 0.6 |
| P2 | H2 — cost scaling | At >= 4 sources, A uses >= 30% fewer invocations than E; the gap widens monotonically with source count through the 12-source cell | 0.65 |
| P3 | H3 — small-scale parity | At <= 2 sources, E matches A's invocation count within 10% — the workspace's coordination buys nothing measurable in a two-feed deployment | 0.6 |
| P4 | H4 — untuned-source robustness | The mid-window new source's planted incident: A catches it with zero config change; E at its documented default threshold either misses it or floods (>= 3x its tuned per-source wake rate). Least certain — a lucky default threshold could make E look robust on this corpus | 0.55 |

## What would falsify the core bet

If E ties A on detection (P1, expected) AND matches A's invocation count at
high source counts (P2 fails) AND handles the untuned source cleanly (P4
fails), then shared arbitration is decoration at every scale tested: the
honest conclusion is that per-source gates + a digest are the right build
below (at least) 12 sources, and TR-2026-03 plus any outreach materials must
say exactly that. The architecture's remaining defensible claims would be
EXP-001/002's cost result vs POLLING only — a claim gates share — plus
whatever the value-of-wakes experiment (future) earns on its own.

## Expected honest headline (written before data)

"Per-source gates match shared arbitration on detection at small scale;
arbitration pays from ~4 sources up (measured >= 30% fewer invocations) and
survives configuration drift that breaks static thresholds." If the data
says otherwise, the data wins.
