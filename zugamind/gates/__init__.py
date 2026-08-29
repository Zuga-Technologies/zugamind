"""Gates layer: the checks that can say NO.

Two are load-bearing on the live cycle, one ships dark, five are opt-in
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
  LIBRARY llm_judge          local-model backstop for confabulated posts
  LIBRARY operational_truth  freshness gate against live service probes
  LIBRARY integrity          longitudinal drift: stationarity, trend, shift
  LIBRARY self_mod_cooldown  restart-durable per-file self-modification lock
  LIBRARY share_filter       share-worthiness screen for outbound thoughts

A LIBRARY gate does no I/O and raises no alerts on its own: the deployer
calls it and routes its verdict. That is deliberate for these five — but it
also means none of them is protecting anything until someone wires it up.
"""
