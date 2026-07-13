# EXP-005 — Value of wakes: does waking the model produce real work?

**Status:** DESIGNED, observational window opens at this commit. Predictions
pre-registered in [exp-005-predictions.md](exp-005-predictions.md) before any
scoring.

## Motivation

EXP-001 through 004 proved the perception layer: cheap, complete, self-honest
attention. None of them touch the claim the product actually rests on — that
a wake produces *work worth having*, not merely a correct ACT line. The
instruments exist in the codebase unmeasured (`gates/work_claim.py` checks a
wake's accomplishment claims against real git history; `gates/value_gate.py`
scores wakes), and a live deployment now generates the data: the BugaPC
dogfood daemon (running since 2026-07-12) writes a self-graded note for every
wake (verdict: worth-it / borderline / noise).

## Design — observational, stated plainly

This is NOT a controlled experiment. It is a scored observation window over a
real deployment: **7 days from this commit**, all wakes of the BugaPC daemon
(scanners: watched-repo issues, repo events, tester crashes, HN/Reddit with
brand watch).

### Outcome definition (deterministic, human-verifiable)

A wake is **VALUABLE** if, within 48 hours, the item it surfaced led to at
least one of:

- (a) a commit or PR that references the surfaced item (issue number, crash
  id, event), verifiable in git history;
- (b) an issue opened, closed, or substantively commented on the surfaced
  item;
- (c) a recorded operator decision that cites the wake (e.g. the wake note's
  recommendation was explicitly acted on or explicitly declined-with-reason).

Everything else is NON-VALUABLE, including wakes that were "interesting."
Interesting-but-actionless is the failure mode this experiment exists to
count honestly.

### Axes measured

1. **Outcome value rate:** valuable wakes / total wakes.
2. **Self-grade calibration:** agreement between the woken instance's own
   verdict (worth-it / noise) and the outcome grade — does the system know
   when it wasted a wake?
3. **Wake economics:** wakes/day, noise rate, and the counterfactual note
   (which valuable items would ALSO have been found by the operator unaided —
   scored by the operator, disclosed as subjective).

### Known circularity, disclosed up front

The operator acts partly BECAUSE a wake told him to — criterion (c) can be
self-fulfilling. We do not pretend otherwise: that circularity IS the product
working (attention directs action). The guard is criterion granularity:
merged code and closed issues (a, b) are hard outcomes; (c) is reported
separately so readers can discount it.

## Scope guards

- Window: 7 days from the pre-registration commit. Wakes before this commit
  (including the 2026-07-12 issue-triage wake that led to the #6 fix) are
  PRIOR evidence, cited as motivation, NOT counted in the scored window.
- Single deployment, single operator, small N — this is a pilot that earns
  (or kills) a multi-week or multi-machine EXP-005b, not a general claim.
- A quiet week (few events, few wakes) is a null-ish result and publishes as
  such; we do not extend the window to fish for wakes.

## Timeline

Window opens at this commit (2026-07-13), closes 2026-07-20; scoring at the
Sunday review 2026-07-20 and publishes within 2 days, either way.
