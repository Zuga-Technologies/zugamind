"""PriorityGoalsModule with a REAL goal list and an actionable cap.

Measured on the BugaPC twin, 2026-08-29 to 2026-09-11 (journal.jsonl): the
module won 1,050 lottery cycles and bought zero sessions, because its bid is
capped at 0.55 under a learned wake floor of 0.57, and because the list it
bids on is the three illustrative example goals shipped with the package,
not the deployment's own. This file pins the two fixes:

  1. set_goals() replaces the list at runtime (a private deployment feeds
     its real goals), keeping touch state for keys that survive.
  2. An ACTIONABLE goal may bid up to SALIENCE_CAP_ACTIONABLE (0.75, parity
     with WorldSignals), on a 24-hour staleness ramp, for at most
     ACTIONABLE_LIMIT goals in list (priority) order. Non-actionable goals
     keep the old 0.55 cap and 12-hour ramp, so an example deployment
     behaves exactly as before.

Run: python -m pytest tests/cognition/workspace/test_priority_goals_real.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta

from cognition.workspace.workspace import SalienceBid, ThoughtType, WorkspaceContent
from cognition.workspace.workspace_modules import PriorityGoalsModule

T0 = datetime(2026, 9, 11, 0, 0, 0)


def _module(now=T0):
    return PriorityGoalsModule(now_fn=lambda: now)


def _touch(m, key, when):
    m._goal_last_touched[key] = when


def test_set_goals_replaces_the_list_and_keeps_known_touch_state():
    m = _module()
    old_key = m.GOALS[0][0]
    _touch(m, old_key, T0 - timedelta(hours=1))
    m.set_goals([(old_key, "kept goal"), ("goal:48", "Dominion buildout")])
    assert [g[0] for g in m.goals()] == [old_key, "goal:48"]
    assert m._goal_last_touched[old_key] == T0 - timedelta(hours=1)
    assert m._goal_last_touched["goal:48"] is None


def test_set_goals_accepts_dicts_with_actionable_flag():
    m = _module()
    m.set_goals([{"key": "goal:1", "label": "one", "actionable": True},
                 {"key": "goal:2", "label": "two"}])
    assert m.is_actionable("goal:1") is True
    assert m.is_actionable("goal:2") is False


def test_non_actionable_goal_keeps_the_old_cap():
    m = _module()
    m.set_goals([("goal:1", "one")])
    _touch(m, "goal:1", T0 - timedelta(hours=40))
    bid = m.generate_bid({})
    assert bid.context["goal_key"] == "goal:1"
    assert bid.salience <= PriorityGoalsModule.SALIENCE_CAP == 0.55


def test_actionable_goal_ramps_to_0_75_at_24_hours():
    m = _module()
    m.set_goals([{"key": "goal:1", "label": "one", "actionable": True}])
    _touch(m, "goal:1", T0 - timedelta(hours=24))
    bid = m.generate_bid({})
    assert abs(bid.salience - PriorityGoalsModule.SALIENCE_CAP_ACTIONABLE) < 1e-9
    assert PriorityGoalsModule.SALIENCE_CAP_ACTIONABLE == 0.75
    assert bid.context["actionable"] is True


def test_actionable_goal_is_below_cap_when_fresh():
    m = _module()
    m.set_goals([{"key": "goal:1", "label": "one", "actionable": True}])
    _touch(m, "goal:1", T0 - timedelta(hours=2))
    bid = m.generate_bid({})
    assert 0.25 < bid.salience < 0.40


def test_actionable_never_touched_bids_above_warmup_floor_but_below_cap():
    """A real goal never advanced this session earns one session soon, but
    must not outrank a goal that has genuinely gone a day without progress."""
    m = _module()
    m.set_goals([{"key": "goal:1", "label": "one", "actionable": True}])
    bid = m.generate_bid({})
    assert bid.context["hours_stale"] == 9999.0
    assert 0.55 <= bid.salience < PriorityGoalsModule.SALIENCE_CAP_ACTIONABLE


def test_actionable_limit_applies_in_list_order():
    m = _module()
    goals = [{"key": f"goal:{i}", "label": f"g{i}", "actionable": True} for i in range(1, 6)]
    m.set_goals(goals)
    n = PriorityGoalsModule.ACTIONABLE_LIMIT
    assert n == 3
    assert [m.is_actionable(f"goal:{i}") for i in range(1, 6)] == [True, True, True, False, False]
    # the fourth goal, though flagged, bids under the old cap
    for i in range(1, 6):
        _touch(m, f"goal:{i}", T0 - timedelta(hours=40))
    _touch(m, "goal:4", T0 - timedelta(hours=60))  # most stale of all
    bid = m.generate_bid({})
    assert bid.context["goal_key"] == "goal:4"
    assert bid.salience <= 0.55


def test_default_example_deployment_is_unchanged():
    m = _module()
    for key, _ in m.GOALS:
        assert m.is_actionable(key) is False
    _touch(m, m.GOALS[0][0], T0 - timedelta(hours=40))
    bid = m.generate_bid({})
    assert bid.salience <= 0.55


def test_on_broadcast_still_resets_a_real_goal():
    m = _module()
    m.set_goals([{"key": "goal:1", "label": "one", "actionable": True}])
    _touch(m, "goal:1", T0 - timedelta(hours=30))
    bid = m.generate_bid({})
    m.on_broadcast(WorkspaceContent(bid=SalienceBid(
        m.name, "x", bid.salience, ThoughtType.METACOGNITION, context=dict(bid.context))))
    assert m._goal_last_touched["goal:1"] == T0
