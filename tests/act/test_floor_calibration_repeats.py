"""One event, one ambient sample: a (module, target) that wins again on the
very next cycle is the same event still on the table, not fresh evidence.

Why this exists (2026-09-11): the learned floor is the 90th percentile of the
last 50 winners plus 0.05. A goal that is two days stale bids 0.75 every
cycle until a session is bought for it; if the harness is capped or paused
for five cycles, five 0.75 samples put the floor at 0.80, above every
module's cap, and the gate is locked for good — the goal keeps winning at
0.75, keeps being sampled, and the floor never comes down. Collapsing a
consecutive repeat of the same target to one sample closes that loop.
Measured on the BugaPC journal (2,048 cycles, 2026-08-29..09-11): no
world_signals winner ever repeated consecutively and no consecutive repeat
of any module bid >= 0.5, so the floor's history is unchanged. Modules that
set no "target" in their bid context are untouched.

Run: python -m pytest tests/act/test_floor_calibration_repeats.py -v
"""
from __future__ import annotations

import act.floor_calibration as fc


def _patch(tmp_path, monkeypatch):
    monkeypatch.setattr(fc, "STATE_FILE", tmp_path / "floor_calibration.json")


def _hc():
    return {"name": "cc", "wake_min_salience": "calibrate"}


def _winner(module="priority_goals", target=None, raw=0.75):
    ctx = {"raw_salience": raw}
    if target is not None:
        ctx["target"] = target
    return {"source_module": module, "salience": raw, "content": "x", "context": ctx}


def _raw():
    return fc._load_state()["cc"]["raw_samples"]


def _modulated():
    return fc._load_state()["cc"]["samples"]


def test_the_same_target_winning_five_cycles_running_is_one_sample(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    for _ in range(5):
        fc.maybe_record_ambient_sample(_hc(), _winner(target="goal:47"))
    assert _raw() == [0.75]
    assert _modulated() == [0.75]


def test_alternating_targets_are_each_sampled(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    for t in ("goal:47", "goal:43", "goal:47"):
        fc.maybe_record_ambient_sample(_hc(), _winner(target=t))
    assert _raw() == [0.75, 0.75, 0.75]


def test_a_streak_broken_by_another_winner_is_sampled_again(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    for t in ("goal:47", "goal:47", "goal:47"):
        fc.maybe_record_ambient_sample(_hc(), _winner(target=t))
    fc.maybe_record_ambient_sample(_hc(), _winner(module="world_signals", raw=0.4))
    fc.maybe_record_ambient_sample(_hc(), _winner(target="goal:47"))
    assert _raw() == [0.75, 0.4, 0.75]


def test_a_module_without_a_target_is_sampled_every_cycle_as_before(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    for _ in range(3):
        fc.maybe_record_ambient_sample(_hc(), _winner(module="world_signals", raw=0.4))
    assert _raw() == [0.4, 0.4, 0.4]


def test_same_target_under_different_modules_is_not_a_repeat(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    fc.maybe_record_ambient_sample(_hc(), _winner(module="a", target="t", raw=0.3))
    fc.maybe_record_ambient_sample(_hc(), _winner(module="b", target="t", raw=0.3))
    assert _raw() == [0.3, 0.3]


def test_a_stuck_bidder_cannot_lift_the_floor_above_its_own_cap(tmp_path, monkeypatch):
    """The deadlock this closes: 45 quiet cycles, then a two-day-stale goal
    bidding 0.75 for ten cycles while the harness is capped. Without the
    collapse the last 50 samples hold ten 0.75s, p90 = 0.75, floor = 0.80,
    and nothing ever wakes again."""
    _patch(tmp_path, monkeypatch)
    for i in range(45):
        fc.maybe_record_ambient_sample(_hc(), _winner(module="world_signals", raw=0.2))
    for _ in range(10):
        fc.maybe_record_ambient_sample(_hc(), _winner(target="goal:47", raw=0.75))
    floor, basis = fc.resolve_gate("cc")
    assert basis == "raw"
    assert floor < 0.75, floor
    assert _raw().count(0.75) == 1
