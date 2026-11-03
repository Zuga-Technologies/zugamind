"""A goal noted on the tracker has its clock advanced BEFORE the next bid.

hm-#887, seen 2026-11-01 on milestone:75: the daemon fed priority_goals from
the hivemind snapshot, THEN ran the cycle, and the snapshot is rewritten by a
scanner INSIDE the cycle. Every bid therefore read the previous cycle's copy.
Goal #14 (a child of milestone:75) was noted at 12:39:50 UTC; the 12:47 bid
still read milestone:75 as 45.8h stale, fired the alarm and bought a wake that
the note had already made pointless.

The fix is a host hook, StreamRunner.before_bids, called after the scanners and
before the modules bid. This test plays the daemon: a scanner stands in for
scan_hivemind_goals and "notes" the goal mid-scan, and the hook re-feeds the
module from the tracker's copy the way goal_feed.feed_goals does.

Run: python -m pytest tests/stream/test_before_bids_goal_feed.py -v
"""
from __future__ import annotations

import json
from datetime import datetime

import act.command_actuator as command_actuator
from stream.runner import StreamRunner

KEY = "milestone:75"
NOW = datetime(2026, 11, 1, 12, 47, 43)             # naive UTC, the module's frame
LAST_WAKE = "2026-10-30T13:58:54+00:00"             # 09:58:54 EDT: before the Nov 1 fall-back
NOTED = "2026-11-01T12:39:50+00:00"                 # goal #14 noted on the tracker


def _setup(monkeypatch, hook):
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [])
    tracker = {KEY: LAST_WAKE}

    def scan_hivemind_goals():
        tracker[KEY] = NOTED       # the snapshot is refreshed inside the cycle
        return []

    runner = StreamRunner(dry_run=True, include_default_scanners=False,
                          extra_scanners={"hivemind_goals": scan_hivemind_goals})
    module = next(m for m in runner.modules if m.name == "priority_goals")
    module._now_fn = lambda: NOW

    def feed():                    # what the daemon's goal_feed.feed_goals does
        module.set_goals([{"key": KEY, "label": "Three-year goal", "actionable": True,
                           "touched": tracker[KEY]}])

    feed()                         # last cycle's copy: the old order's only feed
    if hook:
        runner.before_bids = feed
    return runner, module


def _bid_hours_stale(runner):
    bid = next(b for b in runner.workspace.last_cycle_bids if b.source_module == "priority_goals")
    return bid.context["hours_stale"]


def test_a_goal_noted_on_hivemind_has_its_clock_advanced_before_the_next_bid(monkeypatch):
    runner, module = _setup(monkeypatch, hook=True)
    assert round(module.generate_bid({}).context["hours_stale"], 1) == 46.8   # what the old order bid on

    runner.run_once()

    assert _bid_hours_stale(runner) == 0.13                  # 12:47:43 - 12:39:50 = 7m53s
    # module.STATE_FILE, not the imported class: test_world_signals_module reloads
    # workspace_modules, so a class imported at collection time is a stale twin.
    on_disk = json.loads(module.STATE_FILE.read_text(encoding="utf-8"))
    assert on_disk[KEY] == NOTED                             # the file the daemon reads back, in UTC
    assert module.generate_bid({}).salience < 0.30           # no stale alarm for it this cycle


def test_a_before_bids_hook_that_raises_does_not_stop_the_cycle(monkeypatch):
    runner, _module = _setup(monkeypatch, hook=False)

    def broken():
        raise RuntimeError("snapshot unreadable")

    runner.before_bids = broken
    runner.run_once()                                        # must not raise
    assert _bid_hours_stale(runner) > 46                     # bid on what it had
