"""Gates layer: the checks that can say NO.

Every gate here has a caller. Five run on the live cycle, three ship dark
behind a flag (the guard runs and journals its verdict; only the effect it
gates is off until the flag is on) — stated here because "a gate exists"
and "a gate runs" are different facts, and only the second one protects you.
Check this map against a grep, not against memory: a stale one is worse
than none, because working code that nothing calls reads exactly like
protection (that was the state of the last three until 2026-08-29).

  WIRED   action_gate    the fail-closed, budget-clamped, human-vetoable
                         chokepoint every paid model call passes through
                         (stream.runner, act.command_actuator, demo).
                         Heads every system prompt with the facet's
                         identity (foundation.identity: SENTINEL on local,
                         DELIBERATIVE on paid) under
                         ZUGAMIND_IDENTITY_PROMPT_ENABLED -- dark, since it
                         changes every live prompt
  WIRED   work_claim     confabulation check — verb/commit matching AND
                         entity grounding. Advisory (journal-only) on every
                         harness reply from stream.runner; BLOCKING on the
                         agent's own self-report from cognition.reports,
                         where it runs before llm_judge
  DARK    value_gate     post-hoc usefulness scoring + pre-auction bid
                         re-weighting; no-op until ZUGAMIND_VALUE_GATE_ENABLED
  WIRED   operational_truth  the VERIFIED LIVE STATE block that grounds an
                         idle-cycle reflection, and the freshness check that
                         refuses to reason on a stale subject
                         (cognition.reflection.engine)
  WIRED   integrity      longitudinal drift: stationarity, significant trend,
                         abrupt shift — reading the floor_drifted series the
                         calibrator was already journaling
                         (cognition.reflection.engine)
  WIRED   llm_judge      local-model backstop on the agent's self-report,
                         after work_claim, fail-open (an absent model is
                         ALLOW + reason judge_unavailable, journaled)
                         (cognition.reports.emit_report <- `zugamind report`)
  DARK    share_filter   share-worthiness screen on every completed
                         reflection; verdict journaled as thought_shared /
                         thought_suppressed with reason; nothing is shared
                         until ZUGAMIND_THOUGHTS_ENABLED, and delivery is
                         the journal only (decision 1, 2026-08-29)
                         (cognition.thoughts.consider_thought <-
                         cognition.reflection.engine)
  DARK    self_mod_cooldown  restart-durable per-file lock, taken ATOMICALLY
                         (try_claim) before
                         every proposal to rewrite a facet's runtime
                         override (DATA_DIR/overrides/<facet>.md); the
                         proposal is recorded and cools the file either way,
                         and is APPLIED only under ZUGAMIND_SELF_MOD_ENABLED
                         (decision 2, 2026-08-29: real, not proposal-only)
                         (cognition.self_mod.propose <- `zugamind self-mod`,
                         and <- cognition.proposer, which turns a grounded
                         SELF reflection into one standing line under
                         ZUGAMIND_SELF_MOD_PROPOSER_ENABLED)

The self-modification lane end to end, and the flag that arms each hop
(all default off; the guard runs and journals regardless):
  reflection (SELF, answered) -> proposer  ZUGAMIND_SELF_MOD_PROPOSER_ENABLED
  proposer -> self_mod.propose (cooldown, audit) -> override APPLIED
                                            ZUGAMIND_SELF_MOD_ENABLED
  override -> identity.get_system_prompt -> action_gate system prompt
                                            ZUGAMIND_IDENTITY_PROMPT_ENABLED
With all three off the agent proposes nothing, writes nothing, and its
prompts carry no persona -- exactly the v0.1.0 behaviour. Each flag is
independently verifiable from the journal before the next is turned on.
"""
