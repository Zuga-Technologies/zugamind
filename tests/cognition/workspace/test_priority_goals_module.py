"""Tests for cognition/workspace/workspace_modules.py — PriorityGoalsModule
persistence.

Regression coverage for the bug diagnosed 3x in zugamind-daemon/wake-notes.md
(07-12 17:47, 07-13 16:31, 07-13 16:39): _goal_last_touched was pure
in-memory, so a process restart between wakes wiped it back to None,
permanently pinning hours_stale at the 9999.0 sentinel. STATE_FILE isolation
to a tmp dir comes from tests/conftest.py's autouse fixture — both instances
constructed in a given test share the SAME tmp_path, so persistence-across-
instances is testable with zero extra patching.
"""
from __future__ import annotations

from cognition.workspace.workspace import SalienceBid, ThoughtType, WorkspaceContent
from cognition.workspace.workspace_modules import PriorityGoalsModule


def _win(module: PriorityGoalsModule, key: str, label: str, idx: int) -> WorkspaceContent:
    bid = SalienceBid(
        module.name, f"goal {key}", 0.5, ThoughtType.METACOGNITION,
        context={"goal_index": idx + 1, "goal_key": key, "goal_label": label, "target": key},
    )
    return WorkspaceContent(bid=bid)


def test_fresh_module_has_max_staleness_for_every_goal():
    m = PriorityGoalsModule()
    bid = m.generate_bid({})
    assert bid.context["hours_stale"] == 9999.0


def test_on_broadcast_resets_staleness_for_the_winning_goal():
    m = PriorityGoalsModule()
    key, label = m.GOALS[0]
    m.on_broadcast(_win(m, key, label, 0))
    assert m._goal_last_touched[key] is not None
    bid = m.generate_bid({})
    # The just-touched goal must no longer be the most-stale winner.
    assert bid.context["goal_key"] != key


def test_on_broadcast_persists_to_disk():
    m = PriorityGoalsModule()
    key, label = m.GOALS[0]
    m.on_broadcast(_win(m, key, label, 0))
    assert m.STATE_FILE.exists()


def test_new_instance_loads_persisted_touch_and_does_not_reset_to_9999():
    # Touch ALL goals so the winner is deterministic across the "restart"
    # below — otherwise m2's winner would naturally be a still-untouched
    # goal (correctly 9999.0), which doesn't exercise the bug this guards.
    m1 = PriorityGoalsModule()
    for idx, (key, label) in enumerate(m1.GOALS):
        m1.on_broadcast(_win(m1, key, label, idx))

    m2 = PriorityGoalsModule()  # simulates the process restart that caused the bug
    for key, _label in m2.GOALS:
        assert m2._goal_last_touched[key] is not None
    bid = m2.generate_bid({})
    assert bid.context["hours_stale"] < 9000  # NOT the 9999.0 sentinel anymore


def test_on_broadcast_ignores_other_modules_bids():
    m = PriorityGoalsModule()
    other = WorkspaceContent(bid=SalienceBid("infrastructure", "x", 0.9, ThoughtType.INFRASTRUCTURE))
    m.on_broadcast(other)
    assert all(v is None for v in m._goal_last_touched.values())
    assert not m.STATE_FILE.exists()


def test_on_broadcast_never_raises_on_malformed_content():
    m = PriorityGoalsModule()
    m.on_broadcast(None)  # must not raise


def test_corrupt_state_file_does_not_crash_init(tmp_path, monkeypatch):
    from cognition.workspace import workspace_modules as wm
    bad_file = tmp_path / "priority_goals_state.json"
    bad_file.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(wm.PriorityGoalsModule, "STATE_FILE", bad_file)

    m = PriorityGoalsModule()  # must not raise
    assert all(v is None for v in m._goal_last_touched.values())


def test_set_goal_state_also_persists():
    from datetime import datetime
    m = PriorityGoalsModule()
    key, _ = m.GOALS[0]
    m.set_goal_state({key: datetime.now()})
    assert m.STATE_FILE.exists()

    m2 = PriorityGoalsModule()
    assert m2._goal_last_touched[key] is not None


# --- UTC clocks (hm-#887) ------------------------------------------------------

def test_staleness_across_the_dst_fall_back_is_real_elapsed_time():
    # milestone:75 on 2026-11-01: last touched 09:58:54 EDT on Oct 30, alarm at
    # 12:47:43 UTC on Nov 1 (07:47 EST, after the 02:00 fall-back). Local naive
    # clocks read 45.8h; the real elapsed time is 46h 48m 49s = 46.81h.
    from datetime import datetime, timezone
    m = PriorityGoalsModule(now_fn=lambda: datetime(2026, 11, 1, 12, 47, 43, tzinfo=timezone.utc))
    m.set_goals([{"key": "milestone:75", "label": "x", "actionable": True,
                  "touched": "2026-10-30T13:58:54+00:00"}])
    assert m.generate_bid({}).context["hours_stale"] == 46.81


def test_state_file_is_written_in_utc_with_an_explicit_offset():
    import json
    from datetime import datetime
    m = PriorityGoalsModule(now_fn=lambda: datetime(2026, 11, 1, 12, 47, 43))
    key, label = m.GOALS[0]
    m.on_dispatched({"context": {"goal_key": key}})
    assert json.loads(m.STATE_FILE.read_text(encoding="utf-8"))[key] == "2026-11-01T12:47:43+00:00"


def test_a_state_file_from_before_the_utc_switch_still_means_the_same_instant():
    # Old files hold NAIVE LOCAL text. Reading them as UTC would shift every
    # clock by the zone offset on the first restart after the upgrade.
    import json
    from datetime import datetime, timezone
    legacy = "2026-11-01T07:53:32"          # written by the old code, local wall time
    m0 = PriorityGoalsModule()
    key = m0.GOALS[0][0]
    m0.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    m0.STATE_FILE.write_text(json.dumps({key: legacy}), encoding="utf-8")
    m = PriorityGoalsModule()
    expected = datetime.fromisoformat(legacy).astimezone(timezone.utc).replace(tzinfo=None)
    assert m._goal_last_touched[key] == expected
    m.set_goal_state({})                     # any save rewrites it in the new format
    assert json.loads(m.STATE_FILE.read_text(encoding="utf-8"))[key].endswith("+00:00")
