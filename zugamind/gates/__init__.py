"""Gates layer: the checks that can say NO.

Four are load-bearing on the live cycle, one ships dark, three are opt-in
libraries with no caller in this repo — stated here because "a gate exists"
and "a gate runs" are different facts, and only the second one protects you.

  WIRED   action_gate    the fail-closed, budget-clamped, human-vetoable
                         chokepoint every paid model call passes through
                         (stream.runner, act.command_actuator, demo)
  WIRED   work_claim     confabulation check on harness output — verb/commit
                         matching AND entity grounding, both advisory
                         (journal-only) from stream.runner
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
  LIBRARY llm_judge          local-model backstop for confabulated posts
  LIBRARY self_mod_cooldown  restart-durable per-file self-modification lock
  LIBRARY share_filter       share-worthiness screen for outbound thoughts

A LIBRARY gate does no I/O and raises no alerts on its own: the deployer
calls it and routes its verdict. The three left here are not merely unwired,
they have no socket in this repo — there is no outbound-post path, and no
self-modification path, for them to sit in front of. Activating them means
building those subsystems first, not adding a call.
"""
