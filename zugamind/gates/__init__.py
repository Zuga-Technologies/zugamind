"""Gates layer: pre-action safety gates — the fail-closed, budget-clamped
chokepoint between the workspace and Claude (action_gate), plus post-hoc
integrity checks (value_gate, work_claim, llm_judge, operational_truth,
self_mod_cooldown, share_filter)."""
