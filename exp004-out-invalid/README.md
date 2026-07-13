# EXP-004 — FIRST RUN SET, INVALID (2026-07-13)

These 24 runs (E×12 banked evening 07-12, A×12 run overnight) are **not a
valid A/E comparison** and are kept as the raw record per protocol.

**The bug:** `build_exp004_corpus.py`'s `_incident()` appended `_failure` to
the incident trigger type (`production_down` → `production_down_failure`), a
leftover from a draft that predated the switch to workspace-routable types.
Unroutable types are silently dropped by `route_triggers_to_modules` — so
condition A structurally could not perceive ANY planted incident, in any
cell, while condition E (which gates on `source_name` + urgency and ignores
the type field) saw every incident normally. A's 0.0 recall and E's 1.0
recall in this set measure the bug, not the architectures.

**Why the smoke didn't catch it:** the smoke's A-side 0.0 was (correctly)
attributed to the documented dry-run scoring blind spot — and the follow-up
verification grep for `EXP004` in the journals returned hits that were the
ORACLE'S OWN COMMAND STRING (its regex contains `ZM-EXP004-C`), not briefing
content. The instrument matched itself. Lesson recorded: when verifying that
a payload reached a channel, grep for content the instrument cannot have
produced.

**Scope note:** an early analysis of this set's smoke attributed A's misses
to module-sharing dampening dynamics; on this corpus that diagnosis was
WRONG (the incidents never entered any module). The independently measured
version of that defect — from EXP-003, on a correctly-typed corpus — is
real and fixed (#11, c466008).

The valid run set lives in `exp004-out/`, on the fixed corpus builder
(incident types verbatim-routable, verified end-to-end: incident → router →
CRITICAL bid 0.858 → alarm lane → id in briefing content).
