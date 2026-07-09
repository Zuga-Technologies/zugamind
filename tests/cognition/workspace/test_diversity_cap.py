"""Target-aware hard diversity cap.

The post-modulation hard ceiling counts by (module, target) identity, not
module alone: a module that rotates through different targets (e.g.
priority_goals cycling through goal keys) reads as healthy diversity, not a
streak; a module that is genuinely stuck on the SAME target gets clamped.
"""
from cognition.workspace.workspace import AttentionSchema, ThoughtType, SalienceBid, _bid_target


def _bid(module, sal, **ctx):
    return SalienceBid(module, "x", sal, ThoughtType.METACOGNITION, 0.0, ctx)


def _schema_with_foci(ids):
    schema = AttentionSchema()
    schema.recent_foci = [{"module": m, "target": t} for m, t in ids]
    return schema


def test_bid_target_reads_context_target_key():
    assert _bid_target("priority_goals", {"target": "g1"}) == "g1"
    assert _bid_target("infrastructure", {}) is None


def test_rotating_identity_not_capped():
    # 4 wins across 4 DIFFERENT targets -> a 5th target has 0 recent wins.
    schema = _schema_with_foci([("priority_goals", "g1"), ("priority_goals", "g2"),
                                ("priority_goals", "g3"), ("priority_goals", "g4")])
    b = _bid("priority_goals", 0.9, target="g5")
    capped = schema.apply_hard_diversity_cap([b])
    assert capped == [] and b.salience == 0.9


def test_stuck_identity_four_wins_hard_capped():
    schema = _schema_with_foci([("priority_goals", "g1")] * 4)
    b = _bid("priority_goals", 0.9, target="g1")
    schema.apply_hard_diversity_cap([b])
    assert b.salience == 0.15


def test_stuck_identity_three_wins_quarter_cap():
    schema = _schema_with_foci([("priority_goals", "g1")] * 3)
    b = _bid("priority_goals", 0.9, target="g1")
    schema.apply_hard_diversity_cap([b])
    assert b.salience == 0.25


def test_module_only_module_still_capped():
    schema = _schema_with_foci([("infrastructure", None)] * 4)
    b = _bid("infrastructure", 0.9)
    schema.apply_hard_diversity_cap([b])
    assert b.salience == 0.15


def test_below_ceiling_not_raised():
    schema = _schema_with_foci([("priority_goals", "g1")] * 4)
    b = _bid("priority_goals", 0.1, target="g1")
    capped = schema.apply_hard_diversity_cap([b])
    assert b.salience == 0.1 and capped == []
