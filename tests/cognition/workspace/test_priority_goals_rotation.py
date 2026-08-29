"""PriorityGoalsModule actually rotates (2026-08-28).

Priority used to be a 0.5-HOUR bonus per rank against staleness measured in
hours, so goal #1 won every cycle once all goals had been touched (283 of
285 wins in a 500-cycle measurement). Priority only breaks ties now, and the
clock is injectable so a replay can be byte-identical.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from cognition.workspace import workspace_modules as wm
from cognition.workspace.workspace import Workspace


def _module(tmp_path, monkeypatch, now_fn):
    monkeypatch.setattr(wm.PriorityGoalsModule, "STATE_FILE", tmp_path / "pg.json")
    return wm.PriorityGoalsModule(now_fn=now_fn)


def test_advances_the_least_recently_touched_goal(tmp_path, monkeypatch):
    t0 = datetime(2026, 8, 28, 12, 0, 0)
    m = _module(tmp_path, monkeypatch, lambda: t0)
    m.set_goal_state({"integrity": t0 - timedelta(minutes=5),
                      "truthfulness": t0 - timedelta(minutes=20),
                      "value": t0 - timedelta(minutes=9)})
    assert m.generate_bid({}).context["goal_key"] == "truthfulness"


def test_exact_tie_breaks_by_priority(tmp_path, monkeypatch):
    t0 = datetime(2026, 8, 28, 12, 0, 0)
    m = _module(tmp_path, monkeypatch, lambda: t0)
    m.set_goal_state({k: t0 - timedelta(minutes=10) for k in ("integrity", "truthfulness", "value")})
    assert m.generate_bid({}).context["goal_key"] == "integrity"


def test_rotates_at_a_seven_minute_cadence(tmp_path, monkeypatch):
    clock = {"t": datetime(2026, 8, 28, 12, 0, 0)}
    m = _module(tmp_path, monkeypatch, lambda: clock["t"])
    ws = Workspace()
    ws.register_module(m)
    seen = []
    for _ in range(9):
        seen.append(ws.run_cycle({}).bid.context["goal_key"])
        clock["t"] += timedelta(minutes=7)
    assert seen[:3] == ["integrity", "truthfulness", "value"]
    assert seen.count("integrity") == 3 and seen.count("value") == 3


def test_frozen_clock_makes_bids_byte_identical(tmp_path, monkeypatch):
    frozen = datetime(2026, 8, 28, 12, 0, 0)
    a = _module(tmp_path, monkeypatch, lambda: frozen)
    b = wm.PriorityGoalsModule(now_fn=lambda: frozen)
    a.set_goal_state({"integrity": frozen - timedelta(hours=1)})
    b.set_goal_state({"integrity": frozen - timedelta(hours=1)})
    assert a.generate_bid({}).salience == b.generate_bid({}).salience


def test_default_clock_is_the_real_one(tmp_path, monkeypatch):
    m = _module(tmp_path, monkeypatch, None)
    assert m._now_fn == datetime.now  # bound methods compare equal, not identical
