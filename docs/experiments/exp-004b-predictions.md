# EXP-004b — Pre-registered predictions

**Committed 2026-07-13, before any EXP-004b corpus or harness code exists.**
Design: [exp-004b-whisper-regime.md](exp-004b-whisper-regime.md). Context
available when written: EXP-004/004t complete (detection ties, cost parity
at 1 knob vs S); no whisper-regime data of any kind exists.

| # | Prediction | Confidence |
|---|---|---|
| P1 | THE BET: at heavy overlap, condition A's whisper recall beats condition E's by >= 20 percentage points — arbitration finds recurring whispers a one-dimensional threshold structurally cannot | 0.5 |
| P2 | E's recall degrades monotonically as overlap increases, tracking its threshold's unavoidable recall/flood tradeoff; at clean separation E ties A (the EXP-004 result reproduces at the whisper floor) | 0.65 |
| P3 | A pays for listening: A's invocations exceed E's at heavy overlap, possibly by 2x or more — finding whispers is not free, and we say so up front rather than discover it in the replies | 0.6 |
| P4 | The persistence assumption is load-bearing: in the one-shot-whisper variant cell, A's advantage over E shrinks below 10pp — integration over recurrence, not single-glance brilliance, is where arbitration wins if it wins | 0.55 |

## What would falsify the architecture's detection story — permanently

If P1 fails (A's recall advantage at heavy overlap is under 10pp), then
across EXP-003, EXP-004, and EXP-004b the workspace has failed to show a
detection advantage over simpler designs in every regime tested, including
the one chosen as its best case. The documentation and all outreach then
retire "smarter detection" as a claim entirely; the architecture's evidenced
value is the EXP-002 anti-polling economics and the EXP-004t
one-knob-vs-S-knobs operational story, full stop. We pre-commit to writing
exactly that.
