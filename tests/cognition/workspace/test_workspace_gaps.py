"""cognition/workspace — the 2026-08-28 audit gaps.

Critical world signals reaching the alarm lane, late-registered modules
being routed, wrong-typed trigger fields not erasing a module's bid, the
workspace supplying its own attention state to modules, and a poisoned
attention-schema snapshot degrading instead of raising.
"""
from __future__ import annotations

import logging

import pytest

from cognition.workspace import workspace_modules as wm
from cognition.workspace.module_helpers import self_register
from cognition.workspace.workspace import (
    AttentionSchema, SalienceBid, ThoughtType, Workspace, WorkspaceModule,
)


# --- alarm lane reachability --------------------------------------------------

def test_critical_world_signal_reaches_the_alarm_lane():
    ws = wm.WorldSignalsModule()
    ws.set_triggers([{"type": "hackernews_story", "detail": "prod is down", "relevance": 1.0, "urgency": 0.95},
                     {"type": "reddit_ai_post", "detail": "meh", "relevance": 0.2, "urgency": 0.1}])
    bid = ws.generate_bid({})
    assert Workspace._is_critical(bid) is True
    assert [t["detail"] for t in bid.context["triggers"]] == ["prod is down"]


def test_world_signal_bid_carries_at_most_five_lane_triggers():
    ws = wm.WorldSignalsModule()
    ws.set_triggers([{"type": "hackernews_story", "detail": f"c{i}", "relevance": 0.5, "urgency": 0.95}
                     for i in range(12)])
    assert len(ws.generate_bid({}).context["triggers"]) == 5


def test_quiet_world_signal_is_not_critical():
    ws = wm.WorldSignalsModule()
    ws.set_triggers([{"type": "hackernews_story", "detail": "x", "relevance": 0.5, "urgency": 0.3}])
    assert Workspace._is_critical(ws.generate_bid({})) is False


# --- routing ----------------------------------------------------------------------

class _FooModule(WorkspaceModule):
    name = "foo_late"
    TRIGGER_TYPES = {"foo_event"}

    def generate_bid(self, context):
        return None


def test_late_registered_module_is_routed(monkeypatch):
    monkeypatch.setattr(wm, "ALL_MODULES", list(wm.ALL_MODULES))
    monkeypatch.setattr(wm, "TRIGGER_TYPE_TO_MODULE", dict(wm.TRIGGER_TYPE_TO_MODULE))
    self_register(_FooModule)
    mods = wm.create_all_modules()
    foo = next(m for m in mods if m.name == "foo_late")
    wm.route_triggers_to_modules([{"type": "foo_event", "detail": "x"}], mods)
    assert foo._triggers == [{"type": "foo_event", "detail": "x"}]
    assert wm.TRIGGER_TYPE_TO_MODULE["foo_event"] == "foo_late"


def test_routing_uses_the_modules_passed_in_not_the_frozen_map():
    foo = _FooModule()
    wm.route_triggers_to_modules([{"type": "foo_event"}], [foo])
    assert foo._triggers == [{"type": "foo_event"}]


def test_duplicate_trigger_type_claim_is_loud_and_first_wins(caplog):
    class _Dup(WorkspaceModule):
        name = "dup"
        TRIGGER_TYPES = {"repo_issue"}

        def generate_bid(self, context):
            return None
    repo, dup = wm.RepoIssuesModule(), _Dup()
    with caplog.at_level(logging.WARNING):
        wm.route_triggers_to_modules([{"type": "repo_issue", "issue_title": "t"}], [repo, dup])
    assert len(repo._triggers) == 1 and dup._triggers == []
    assert any("claimed by both" in r.message for r in caplog.records)


# --- wrong-typed trigger fields ----------------------------------------------

@pytest.mark.parametrize("module_cls,trigger", [
    (wm.RepoIssuesModule, {"type": "repo_issue", "issue_title": None, "issue_number": 7, "repo": "r"}),
    (wm.CodeChangeModule, {"type": "git_commit", "detail": None}),
    (wm.WorldSignalsModule, {"type": "hackernews_story", "detail": "x", "relevance": "0.7", "urgency": None}),
    (wm.InfrastructureModule, {"service": "db"}),                       # no "type" at all
    (wm.DaemonModule, {"type": "daemon_task_failed", "detail": 42}),
    (wm.ScheduleModule, {"type": "analytics_significant", "detail": None}),
    (wm.KnowledgeModule, {"type": "vault_change", "file": None}),
])
def test_wrong_typed_trigger_field_does_not_erase_the_bid(module_cls, trigger):
    m = module_cls()
    m.set_triggers([trigger])
    bid = m.generate_bid({})
    assert bid is not None and bid.is_valid


def test_string_relevance_is_coerced_not_dropped():
    ws = wm.WorldSignalsModule()
    ws.set_triggers([{"type": "hackernews_story", "detail": "x", "relevance": "1.0", "urgency": "0"}])
    assert ws.generate_bid({}).salience == pytest.approx(min(0.75, 0.25 + 0.4))


# --- the workspace supplies its own attention state --------------------------

class _Echo(WorkspaceModule):
    name = "echo"
    seen = None

    def generate_bid(self, context):
        _Echo.seen = dict(context)
        return SalienceBid(self.name, "echo", 0.5, ThoughtType.KNOWLEDGE)


def test_run_cycle_stamps_last_winner_and_stuck_into_the_context():
    ws = Workspace()
    ws.register_module(_Echo())
    ws.run_cycle({"trigger_count": 0})
    assert _Echo.seen["last_winner_module"] == "" and _Echo.seen["attention_stuck"] is False
    for _ in range(3):
        ws.run_cycle({})
    assert _Echo.seen["last_winner_module"] == "echo"
    assert _Echo.seen["attention_stuck"] is True  # the same identity won 3 in a row
    ws.run_cycle({"attention_stuck": "caller-says"})
    assert _Echo.seen["attention_stuck"] == "caller-says"  # a caller-supplied value wins


def test_metacognition_reads_the_stuck_signal_from_the_context():
    m = wm.MetacognitiveModule()
    calm = m.generate_bid({"attention_stuck": False, "last_winner_module": "x"})
    stuck = m.generate_bid({"attention_stuck": True, "last_winner_module": "x"})
    yielding = m.generate_bid({"attention_stuck": True, "last_winner_module": "metacognition"})
    assert calm.salience == 0.1
    assert stuck.salience >= 0.3 and "stuck" in stuck.content
    assert yielding.salience == 0.0 and "yielding" in yielding.content


# --- poisoned attention snapshot --------------------------------------------------

def test_restore_from_dict_coerces_a_poisoned_snapshot():
    a = AttentionSchema()
    a.restore_from_dict({
        "current_focus": 5, "current_focus_module": None, "current_focus_target": 3,
        "recent_foci": "nope", "module_win_counts": [1, 2], "attention_switches": "9",
        "total_cycles": True, "adjustments": {"m": "x", "k": 0.1},
    })
    assert a.current_focus == "" and a.current_focus_module == "" and a.current_focus_target is None
    assert a.recent_foci == [] and a.module_win_counts == {} and a.attention_switches == 0
    assert a._total_cycles == 0 and a._adjustments == {"k": 0.1}
    # and the schema still runs a cycle
    ws = Workspace()
    ws.attention_schema = a
    ws.register_module(_Echo())
    assert ws.run_cycle({}) is not None


def test_restore_from_dict_round_trips_a_real_snapshot():
    ws = Workspace()
    ws.register_module(_Echo())
    for _ in range(4):
        ws.run_cycle({})
    ws.attention_schema.set_adjustment("echo", -0.05)       # non-empty adjustments...
    ws.attention_schema.current_focus_target = "goal-x"     # ...and a real target round-trip too
    snap = ws.attention_schema.to_dict()
    b = AttentionSchema()
    b.restore_from_dict(snap)
    assert b.to_dict() == snap


# --- priority goals state -----------------------------------------------------------

def test_offset_aware_persisted_touch_does_not_kill_the_bid(tmp_path, monkeypatch):
    monkeypatch.setattr(wm.PriorityGoalsModule, "STATE_FILE", tmp_path / "pg.json")
    (tmp_path / "pg.json").write_text(
        '{"integrity": "2026-08-28T10:00:00+00:00", "truthfulness": null, "value": null}', encoding="utf-8")
    m = wm.PriorityGoalsModule()
    bid = m.generate_bid({})
    assert bid is not None and bid.is_valid
    assert m._goal_last_touched["integrity"].tzinfo is None


def test_one_corrupt_key_does_not_lose_the_others(tmp_path, monkeypatch):
    monkeypatch.setattr(wm.PriorityGoalsModule, "STATE_FILE", tmp_path / "pg.json")
    (tmp_path / "pg.json").write_text(
        '{"integrity": "NOT-A-DATE", "truthfulness": "2026-08-28T10:00:00", "value": 12}', encoding="utf-8")
    m = wm.PriorityGoalsModule()
    assert m._goal_last_touched["integrity"] is None
    assert m._goal_last_touched["truthfulness"] is not None
    assert m._goal_last_touched["value"] is None


# --- content length is bounded ------------------------------------------------------

@pytest.mark.parametrize("module_cls,trigger", [
    (wm.InfrastructureModule, {"type": "local_service_down", "detail": "x" * 50_000}),
    (wm.InfrastructureModule, {"type": "production_degraded", "detail": "x" * 50_000}),
    (wm.DaemonModule, {"type": "daemon_task_failed", "detail": "x" * 50_000}),
    (wm.CodeChangeModule, {"type": "git_commit", "detail": "fix", "project": "p" * 50_000}),
    (wm.RepoIssuesModule, {"type": "repo_issue", "issue_title": "t" * 50_000, "repo": "r" * 50_000}),
    (wm.KnowledgeModule, {"type": "vault_change", "file": "f" * 50_000}),
    (wm.ScheduleModule, {"type": "analytics_significant", "detail": "d" * 50_000}),
    (wm.WorldSignalsModule, {"type": "hackernews_story", "detail": "w" * 50_000, "relevance": 0.5, "urgency": 0.1}),
])
def test_bid_content_is_bounded(module_cls, trigger):
    m = module_cls()
    m.set_triggers([trigger])
    assert len(m.generate_bid({}).content) < 1000


# --- core engine ------------------------------------------------------------------

class _BadType(WorkspaceModule):
    name = "badtype"

    def generate_bid(self, context):
        return SalienceBid(self.name, "oops", 0.9, "not-a-thought-type")  # type: ignore[arg-type]


def test_bid_with_a_non_thought_type_is_invalid_and_never_wins():
    ws = Workspace()
    ws.register_module(_BadType())
    ws.register_module(_Echo())
    for _ in range(5):
        assert ws.run_cycle({}).source_module == "echo"


def test_post_broadcast_log_line_cannot_raise():
    """A modulator that corrupts thought_type after validation must not crash
    the cycle after broadcast/update have already run."""
    ws = Workspace()
    ws.register_module(_Echo())

    def corrupt(bids, ctx):
        for b in bids:
            b.thought_type = "corrupted"
        return bids
    ws.register_modulator(corrupt)
    content = ws.run_cycle({})
    assert content is not None and content.broadcast_complete is True


class _Chronic(WorkspaceModule):
    name = "chronic"

    def generate_bid(self, context):
        return SalienceBid(self.name, "never wins", 0.02, ThoughtType.KNOWLEDGE)


class _Echo2(_Echo):
    name = "echo2"


def test_a_module_that_never_won_is_still_a_blind_spot():
    """With TWO healthy competitors the streak dampening never has to hand a
    cycle to the 0.02 bidder (with one competitor it eventually does — that
    is the dampening working, not the blind-spot boost)."""
    import random
    random.seed(7)
    ws = Workspace()
    ws.register_module(_Chronic())
    ws.register_module(_Echo())
    ws.register_module(_Echo2())
    for _ in range(12):
        ws.run_cycle({})
    assert ws.attention_schema.module_win_counts["chronic"] == 0
    assert "chronic" in ws.attention_schema.blind_spots


class _Silent(WorkspaceModule):
    name = "silent"
    speak = False

    def generate_bid(self, context):
        if not _Silent.speak:
            return None
        return SalienceBid(self.name, "first words", 0.3, ThoughtType.KNOWLEDGE)


def test_a_module_that_never_bid_is_not_a_blind_spot():
    """Seeding at registration over-corrected: a module that had merely been
    silent got the 1.4x rescue on its first-ever bid (a 0.3 bid won at 0.924
    with the boosts stacked). Only a module that has competed is seeded."""
    import random
    random.seed(3)
    _Silent.speak = False
    ws = Workspace()
    ws.register_module(_Silent())
    ws.register_module(_Echo())
    ws.register_module(_Echo2())
    for _ in range(10):
        ws.run_cycle({})
    assert "silent" not in ws.attention_schema.module_win_counts
    assert "silent" not in ws.attention_schema.blind_spots
    _Silent.speak = True
    ws.run_cycle({})
    first = next(b for b in ws.last_cycle_bids if b.source_module == "silent")
    assert first.salience < 0.6                       # no blind-spot rescue on a first bid
    assert "silent" in ws.attention_schema.module_win_counts  # but it is now in the race


def test_duplicate_claim_winner_does_not_depend_on_caller_order(caplog):
    class _Dup2(WorkspaceModule):
        name = "dup2"
        TRIGGER_TYPES = {"repo_issue"}

        def generate_bid(self, context):
            return None
    for order in ([wm.RepoIssuesModule(), _Dup2()], [_Dup2(), wm.RepoIssuesModule()]):
        wm.route_triggers_to_modules([{"type": "repo_issue", "issue_title": "t"}], order)
        repo = next(m for m in order if m.name == "repo_issues")
        dup = next(m for m in order if m.name == "dup2")
        assert len(repo._triggers) == 1 and dup._triggers == []


def test_world_signal_top_fields_are_coerced():
    ws = wm.WorldSignalsModule()
    ws.set_triggers([{"type": 123, "detail": "x", "relevance": 0.5, "urgency": 0.1, "url": 42}])
    ctx = ws.generate_bid({}).context
    assert ctx["top_type"] == "123" and ctx["top_url"] == "42"


def test_stats_expose_the_alarm_refractory_clock():
    ws = Workspace()
    ws.register_module(_Echo())
    ws.run_cycle({})
    stats = ws.get_stats()
    assert stats["selection_cycle"] == 1 and stats["served_alarms"] == {}
    assert ws.run_cycle({}) is not None
    ws._modules.clear()               # an idle cycle: no bids
    assert ws.run_cycle({}) is None
    stats = ws.get_stats()
    assert stats["cycle_count"] == 3 and stats["selection_cycle"] == 2


# --- planner / actuator / helpers -----------------------------------------------------

def test_format_plan_for_prompt_tolerates_hand_built_steps():
    from cognition.workspace.workspace_planner import WorkspacePlanner
    text = WorkspacePlanner().format_plan_for_prompt([{"foo": "bar"}, "not a dict", {"action": "code", "description": "d"}])
    assert "1. [?]" in text and "2. [?]" in text and "3. [code] d" in text


def test_runner_hands_the_planner_the_real_budget():
    """A hardcoded {"remaining": 10.0} meant the planner's low-budget clamp
    could never fire; the runner now loads the real ledger (the stub is the
    fallback when the ledger cannot be read)."""
    from pathlib import Path as _P
    from stream import runner as runner_mod
    src = _P(runner_mod.__file__).read_text(encoding="utf-8")
    assert "budget = load_budget()" in src
    assert 'budget = {"remaining": 10.0}' in src.split("budget = load_budget()")[1]  # fallback only


def test_actuator_state_file_is_validated(tmp_path, monkeypatch):
    from cognition.workspace import workspace_actuator as wa
    monkeypatch.setattr(wa, "ACTUATOR_STATE_FILE", tmp_path / "actuator_state.json")
    monkeypatch.setattr(wa, "ENGINE_DIR", tmp_path)
    (tmp_path / "actuator_state.json").write_text("[1, 2, 3]", encoding="utf-8")
    wa.WorkspaceActuator()  # used to raise AttributeError
    (tmp_path / "actuator_state.json").write_text('{"cycles_since_check": "oops", "total_checks": -4}', encoding="utf-8")
    a = wa.WorkspaceActuator()
    assert a._cycles_since_check == 0 and a._total_checks == 0
    assert a.on_cycle_complete({}, AttentionSchema(), 1) == {}  # used to TypeError on += 1


def test_ro_conn_handles_hash_and_space_in_the_path(tmp_path):
    """`#` used to truncate the path (URI fragment) and open a different,
    empty database silently. (`?` would collide with `?mode=ro` too, but it
    is not a legal filename on Windows, so it cannot be exercised here.)"""
    import sqlite3
    from cognition.workspace.module_helpers import ro_conn
    for name in ("my#db.sqlite", "with space.sqlite"):
        db = tmp_path / name
        with sqlite3.connect(db) as w:
            w.execute("create table t(x)"); w.execute("insert into t values (42)"); w.commit()
        with ro_conn(db) as r:
            assert r.execute("select x from t").fetchone() == (42,)
            with pytest.raises(sqlite3.OperationalError):
                r.execute("insert into t values (1)")  # read-only really is read-only
