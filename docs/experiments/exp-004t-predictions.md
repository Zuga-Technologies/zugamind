# EXP-004t — Pre-registered predictions (tuned-floor addendum to EXP-004)

**Committed 2026-07-13, before the calibration harness exists and before any
measured run.** Immutable from this commit; results publish either way.

## What EXP-004t is

EXP-004's conditions were asymmetrically tuned: condition E's per-source
thresholds were calibrated on a separate calibration corpus (published
procedure), while condition A ran with its factory-default wake floor
(`wake_min_salience 0.35`), tuned to nothing. EXP-004t closes the symmetry
gap: condition **At** = the full workspace with ONE global wake floor
calibrated by the SAME published procedure E's thresholds got (floor = max
ambient *winner* salience observed on the calibration corpus + 0.05 margin;
winner salience, not raw bid salience, because the floor filters winners).
Nothing else changes: same corpora, same seeds, same oracle, same post-#11
architecture (alarm-lane winners bypass the floor by design, which is what
makes a high floor safe for criticals).

Context available when written: EXP-004 valid results (E: recall 1.0
everywhere, 2.7/5/8.7/13 wakes at S=2/4/8/12, 2–13 tuned parameters;
A-default: recall 1.0 everywhere, ~15–40 wakes, 0 parameters). No At data
exists.

| # | prediction | confidence |
|---|---|---|
| P1 | At's recall stays 1.0 at every source count, H4 newcomer included — criticals reach briefings via the floor-bypassing alarm lane and the digest, so the raised floor costs no detection on this corpus | 0.75 |
| P2 | At's invocation count lands within 1.5× of E's at every source count (e.g. ≤ ~20 at S=12 vs E's 13) — the ambient wakes were the whole gap | 0.6 |
| P3 | At does NOT beat E's invocation count at any source count — gates tuned per-source remain the cost floor on a corpus where every alert screams; the honest claim is parity-at-1-knob, not victory | 0.6 |
| P4 | Config-parameter asymmetry stands: At carries exactly 1 calibrated parameter at every scale; E carries S+0 (2/4/8/12 + documented default). At S=12 that is 1 vs 13 | 1.0 (arithmetic) |

## What would falsify the addendum's premise

If P1 fails (raising the floor costs detection anywhere), the #11 bypass is
insufficient and "one tuned knob" is not actually available to the workspace
— EXP-004's as-measured cost loss stands un-mitigated and the results doc
says so. If P2 fails (At still wakes ≥2× E), the ambient-wake explanation
for the gap was wrong and the workspace's cost story vs strong gates is
genuinely weaker than EXP-002's anti-polling story implied.

## Scope guard

At is an EXPERIMENT-harness calibration (a parameter passed to the runner),
not a product code change. The whisper-regime corpus (where arbitration is
hypothesized to beat gates on detection, not just tie) remains EXP-004b,
separately pre-registered, unaffected by this addendum.
