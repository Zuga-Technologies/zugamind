"""PriorityGoalsModule with a REAL goal list, an actionable cap, and a clock
that resets on a DISPATCHED session rather than on a workspace win.

Measured on the BugaPC twin, 2026-08-29 to 2026-09-11 (journal.jsonl, 2,048
cycles): the module won the workspace 1,057 times and bought zero sessions.
The cap (0.55 under a learned floor) was never the binding constraint: 94% of
those wins bid 0.23-0.26 with the goal under an hour stale, because
on_broadcast reset the goal's clock on every workspace WIN, and the module
wins about every other cycle. This file pins the three fixes:

  1. set_goals() replaces the list at runtime (a private deployment feeds
     its real goals), keeping touch state for keys that survive, and may
     carry a "touched" timestamp (the tracker's updated_at) that advances a
     goal's clock but never regresses it.
  2. An ACTIONABLE goal may bid up to SALIENCE_CAP_ACTIONABLE (0.75, parity
     with WorldSignals) on a 48-hour staleness ramp that crosses the floor's
     0.50 clamp minimum at 24 h, for at most ACTIONABLE_LIMIT goals in list
     (priority) order. Non-actionable goals keep the old 0.55 cap and
     12-hour ramp, so an example deployment behaves exactly as before.
  3. An actionable goal's clock resets in on_dispatched() (a harness really
     ran for it), NOT in on_broadcast(). Non-actionable goals keep the old
     reset-on-win semantics.

Run: python -m pytest tests/cognition/workspace/test_priority_goals_real.py -v
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from cognition.workspace.workspace import SalienceBid, ThoughtType, WorkspaceContent
from cognition.workspace.workspace_modules import PriorityGoalsModule

T0 = datetime(2026, 9, 11, 0, 0, 0)


def _module(now=T0):
    return PriorityGoalsModule(now_fn=lambda: now)


def _touch(m, key, when):
    m._goal_last_touched[key] = when


def _broadcast(m, bid):
    m.on_broadcast(WorkspaceContent(bid=SalienceBid(
        m.name, "x", bid.salience, ThoughtType.METACOGNITION, context=dict(bid.context))))


def _winner_for(bid):
    return {"source_module": "priority_goals", "salience": bid.salience,
            "content": bid.content, "context": dict(bid.context)}


# --- 1. the list -------------------------------------------------------------

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


def test_touched_from_the_tracker_sets_a_new_goals_clock_in_local_naive_time():
    m = _module()
    when = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    m.set_goals([{"key": "goal:1", "label": "one", "actionable": True, "touched": when.isoformat()}])
    clock = m._goal_last_touched["goal:1"]
    assert clock is not None and clock.tzinfo is None
    assert clock == when.astimezone().replace(tzinfo=None)


def test_touched_advances_a_clock_but_never_regresses_it():
    m = _module()
    m.set_goals([{"key": "goal:1", "label": "one", "actionable": True}])
    _touch(m, "goal:1", T0 - timedelta(hours=2))
    # older than what the module knows: ignored
    m.set_goals([{"key": "goal:1", "label": "one", "actionable": True,
                  "touched": (T0 - timedelta(hours=30)).isoformat()}])
    assert m._goal_last_touched["goal:1"] == T0 - timedelta(hours=2)
    # newer: the clock moves forward
    m.set_goals([{"key": "goal:1", "label": "one", "actionable": True,
                  "touched": (T0 - timedelta(minutes=10)).isoformat()}])
    assert m._goal_last_touched["goal:1"] == T0 - timedelta(minutes=10)


def test_a_bad_touched_timestamp_does_not_lose_the_goal():
    m = _module()
    m.set_goals([{"key": "goal:1", "label": "one", "actionable": True, "touched": "not a date"},
                 {"key": "goal:2", "label": "two"}])
    assert [g[0] for g in m.goals()] == ["goal:1", "goal:2"]
    assert m._goal_last_touched["goal:1"] is None


# --- 2. the bid --------------------------------------------------------------

def test_non_actionable_goal_keeps_the_old_cap():
    m = _module()
    m.set_goals([("goal:1", "one")])
    _touch(m, "goal:1", T0 - timedelta(hours=40))
    bid = m.generate_bid({})
    assert bid.context["goal_key"] == "goal:1"
    assert bid.salience <= PriorityGoalsModule.SALIENCE_CAP == 0.55


def test_actionable_goal_reaches_0_75_at_48_hours():
    m = _module()
    m.set_goals([{"key": "goal:1", "label": "one", "actionable": True}])
    _touch(m, "goal:1", T0 - timedelta(hours=48))
    bid = m.generate_bid({})
    assert abs(bid.salience - PriorityGoalsModule.SALIENCE_CAP_ACTIONABLE) < 1e-9
    assert PriorityGoalsModule.SALIENCE_CAP_ACTIONABLE == 0.75
    assert bid.context["actionable"] is True


def test_actionable_goal_crosses_the_floor_clamp_minimum_at_24_hours():
    """0.50 is act/floor_calibration.WARMUP_FLOOR, the lowest the learned
    floor can ever be: a day without progress is exactly one session."""
    m = _module()
    m.set_goals([{"key": "goal:1", "label": "one", "actionable": True}])
    _touch(m, "goal:1", T0 - timedelta(hours=24))
    assert abs(m.generate_bid({}).salience - 0.50) < 1e-9
    _touch(m, "goal:1", T0 - timedelta(hours=23))
    assert m.generate_bid({}).salience < 0.50


def test_actionable_goal_is_below_cap_when_fresh():
    m = _module()
    m.set_goals([{"key": "goal:1", "label": "one", "actionable": True}])
    _touch(m, "goal:1", T0 - timedelta(hours=2))
    bid = m.generate_bid({})
    assert 0.25 < bid.salience < 0.40


def test_actionable_never_touched_bids_above_warmup_floor_but_below_cap():
    """A real goal never advanced this session earns one session soon, but
    must not outrank a goal that has genuinely gone two days without progress."""
    m = _module()
    m.set_goals([{"key": "goal:1", "label": "one", "actionable": True}])
    bid = m.generate_bid({})
    assert bid.context["hours_stale"] == 9999.0
    assert 0.55 <= bid.salience < PriorityGoalsModule.SALIENCE_CAP_ACTIONABLE


def _flag5(m):
    m.set_goals([{"key": f"goal:{i}", "label": f"g{i}", "actionable": True} for i in range(1, 6)])
    return m


def test_only_three_flagged_goals_hold_a_slot_at_once():
    m = _flag5(_module())
    assert PriorityGoalsModule.ACTIONABLE_LIMIT == 3
    for i in range(1, 6):
        assert m.is_flagged(f"goal:{i}") is True
    assert sum(m.is_actionable(f"goal:{i}") for i in range(1, 6)) == 3


def test_the_slots_go_to_the_stalest_flagged_goals_not_the_first_in_the_list():
    """The starvation this fixes, found live 2026-09-11 00:45: list order is
    (status, ticket count, key) and never changes, so with fixed-order slots
    the sixth goal could go 109h untouched and still bid under the floor."""
    m = _flag5(_module())
    for i in range(1, 6):
        _touch(m, f"goal:{i}", T0 - timedelta(hours=1))
    _touch(m, "goal:5", T0 - timedelta(hours=90))   # last in the list, stalest
    _touch(m, "goal:4", T0 - timedelta(hours=60))
    _touch(m, "goal:3", T0 - timedelta(hours=50))
    assert [m.is_actionable(f"goal:{i}") for i in range(1, 6)] == [False, False, True, True, True]
    bid = m.generate_bid({})
    assert bid.context["goal_key"] == "goal:5"
    assert bid.context["actionable"] is True
    assert abs(bid.salience - 0.75) < 1e-9


def test_a_slot_rotates_when_the_goal_holding_it_gets_its_session():
    m = _flag5(_module())
    for i, h in ((1, 90), (2, 80), (3, 70), (4, 60), (5, 50)):
        _touch(m, f"goal:{i}", T0 - timedelta(hours=h))
    assert [m.is_actionable(f"goal:{i}") for i in range(1, 6)] == [True, True, True, False, False]
    # goal:1 gets its session; the next-stalest takes the freed slot
    m.on_dispatched({"source_module": "priority_goals", "context": {"goal_key": "goal:1"}})
    assert [m.is_actionable(f"goal:{i}") for i in range(1, 6)] == [False, True, True, True, False]
    m.on_dispatched({"source_module": "priority_goals", "context": {"goal_key": "goal:2"}})
    assert [m.is_actionable(f"goal:{i}") for i in range(1, 6)] == [False, False, True, True, True]


def test_a_flagged_goal_waiting_for_a_slot_keeps_getting_staler():
    """It must: a win resetting its clock would leave it permanently unable
    to out-stale the goals already holding slots."""
    m = _flag5(_module())
    for i, h in ((1, 90), (2, 80), (3, 70), (4, 60), (5, 50)):
        _touch(m, f"goal:{i}", T0 - timedelta(hours=h))
    assert m.is_actionable("goal:5") is False
    _broadcast(m, m.generate_bid({}))          # goal:1 wins the workspace
    assert m._goal_last_touched["goal:5"] == T0 - timedelta(hours=50)
    assert m._goal_last_touched["goal:1"] == T0 - timedelta(hours=90)


def test_a_ninety_hour_goal_last_in_the_list_reaches_the_cap():
    """goal:62's live shape: flagged, bottom of the list, four days stale."""
    m = _module()
    m.set_goals([{"key": f"goal:{i}", "label": f"g{i}", "actionable": True} for i in (14, 47, 43, 56, 62)])
    for k, h in (("goal:14", 1), ("goal:47", 2), ("goal:43", 3), ("goal:56", 4), ("goal:62", 109)):
        _touch(m, k, T0 - timedelta(hours=h))
    bid = m.generate_bid({})
    assert bid.context["goal_key"] == "goal:62"
    assert abs(bid.salience - 0.75) < 1e-9


def test_default_example_deployment_is_unchanged():
    m = _module()
    for key, _ in m.GOALS:
        assert m.is_actionable(key) is False
    _touch(m, m.GOALS[0][0], T0 - timedelta(hours=40))
    bid = m.generate_bid({})
    assert bid.salience <= 0.55


# --- 3. the clock ------------------------------------------------------------

def test_winning_the_workspace_does_not_reset_an_actionable_goal():
    """The 1,057-wins-0-sessions failure: a win is free and advances
    nothing, so it must not restart the ramp."""
    m = _module()
    m.set_goals([{"key": "goal:1", "label": "one", "actionable": True}])
    _touch(m, "goal:1", T0 - timedelta(hours=30))
    bid = m.generate_bid({})
    _broadcast(m, bid)
    assert m._goal_last_touched["goal:1"] == T0 - timedelta(hours=30)
    # and the next cycle bids exactly as high again
    assert abs(m.generate_bid({}).salience - bid.salience) < 1e-9


def test_winning_the_workspace_still_resets_a_non_actionable_goal():
    m = _module()
    m.set_goals([("goal:1", "one")])
    _touch(m, "goal:1", T0 - timedelta(hours=30))
    _broadcast(m, m.generate_bid({}))
    assert m._goal_last_touched["goal:1"] == T0


def test_a_dispatched_session_resets_the_actionable_goal_and_persists():
    m = _module()
    m.set_goals([{"key": "goal:1", "label": "one", "actionable": True}])
    _touch(m, "goal:1", T0 - timedelta(hours=30))
    bid = m.generate_bid({})
    m.on_dispatched(_winner_for(bid))
    assert m._goal_last_touched["goal:1"] == T0
    on_disk = json.loads(PriorityGoalsModule.STATE_FILE.read_text(encoding="utf-8"))
    assert on_disk["goal:1"] == T0.isoformat()
    # the ramp restarts from the session
    assert m.generate_bid({}).salience < 0.30


def test_on_dispatched_ignores_a_winner_for_an_unknown_goal():
    m = _module()
    m.set_goals([{"key": "goal:1", "label": "one", "actionable": True}])
    _touch(m, "goal:1", T0 - timedelta(hours=30))
    m.on_dispatched({"source_module": "priority_goals", "context": {"goal_key": "goal:404"}})
    m.on_dispatched({})
    assert m._goal_last_touched["goal:1"] == T0 - timedelta(hours=30)
