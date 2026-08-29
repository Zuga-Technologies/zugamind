"""stream/runner.py — the 2026-08-29 adversarial audit (three reviewers).

Every test here pins a finding that was CONFIRMED against the code:
- an alarm-lane rescue landed in FOCUSED with no "alarm" event and a
  briefing that read like routine chatter;
- the idle counter reset on every restart, so REFLECTING never came;
- quiet_hours_deferred journaled the full 26 KB winner per harness;
- score_action made a 90 s local-model call per real wake with the value
  gate OFF;
- a raise from harness A stopped harness B and left last_wake stale;
- a future last_wake emptied every briefing;
- a ZUGAMIND_WAKE_TIER typo downgraded silently to PAID haiku;
- a floor the filter could not read meant NO floor;
- a module could forge alarm_lane and bypass every floor;
- ai_labs triggers fell to the text hash (no "link" in the identity keys);
- a broken dynamic scanner was swallowed with no log at any level;
- work_claim always checked zugamind-src's own git history.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import pytest

import act.command_actuator as command_actuator
import act.floor_calibration as floor_calibration
import continuity.journal as journal
import foundation.config as config
import foundation.state as state_mod
import gates.work_claim as work_claim
import scanners as scanners_pkg
import stream.runner as runner_mod
from cognition.workspace.workspace import SalienceBid, Workspace
from stream.runner import StreamRunner


def _toy_scanner():
    return [{"type": "local_service_down", "service": "toy-api", "port": 9999,
             "detail": "toy-api not responding", "urgency": 0.5}]


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    monkeypatch.setattr(journal, "_appends_since_check", 0)
    monkeypatch.setattr(journal, "_tail_checked_for", None)
    monkeypatch.setattr(state_mod, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(state_mod, "ENGINE_DIR", tmp_path)
    monkeypatch.setattr(config, "PAUSE_FILE", tmp_path / "PAUSE")
    monkeypatch.setattr(floor_calibration, "STATE_FILE", tmp_path / "floor.json")
    monkeypatch.setattr(command_actuator, "load_quiet_hours", lambda *a, **kw: None)
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [])
    monkeypatch.delenv("ZUGAMIND_WAKE_TIER", raising=False)
    monkeypatch.delenv("ZUGAMIND_VALUE_GATE_ENABLED", raising=False)
    return tmp_path


def _runner(**kw):
    kw.setdefault("extra_scanners", {"scan_toy": _toy_scanner})
    kw.setdefault("include_default_scanners", False)
    kw.setdefault("dry_run", True)
    return StreamRunner(**kw)


def _events(kind=None):
    evs = journal.read_events(limit=None)
    return [e for e in evs if kind is None or e.get("kind") == kind]


def _harness(name="h", **extra):
    hc = {"name": name, "command": [name, "{briefing_file}"], "timeout_sec": 5,
          "max_per_hour": 100, "max_per_day": 1000, "enabled": True}
    hc.update(extra)
    return hc


# --- alarm lane -> ALERT ---------------------------------------------------------------

def test_alarm_lane_rescue_is_an_alert_even_when_its_salience_is_dampened(engine):
    r = _runner()
    winner = {"source_module": "infrastructure", "content": "prod db down", "salience": 0.15,
              "context": {"alarm_lane": True, "raw_salience": 0.15}}
    state = r._transition_state(winner)
    assert state["state"] == "ALERT"
    alarms = _events("alarm")
    assert len(alarms) == 1 and alarms[0]["alarm_lane"] is True


def test_an_attention_boost_cannot_manufacture_an_alert(engine):
    """Live 2026-08-29 01:35: bid 0.510, boosted to 0.785 -> ALERT + "alarm"
    for a watched-page footer change; the wake gate then refused it on the
    raw 0.51. ALERT judges min(bid, modulated), like the floor does."""
    boosted = {"source_module": "world_signals", "content": "Privacy choices", "salience": 0.785,
               "context": {"raw_salience": 0.510}}
    state = _runner()._transition_state(boosted)
    assert state["state"] == "FOCUSED" and not _events("alarm")
    genuinely_hot = {"source_module": "infrastructure", "content": "db down", "salience": 0.75,
                     "context": {"raw_salience": 0.80}}
    assert _runner()._transition_state(genuinely_hot)["state"] == "ALERT"


def test_plain_low_salience_winner_is_focused_not_alert(engine):
    state = _runner()._transition_state({"source_module": "m", "content": "c", "salience": 0.3, "context": {}})
    assert state["state"] == "FOCUSED" and not _events("alarm")


def test_briefing_names_an_alarm_lane_winner_as_critical(engine):
    text = journal.build_briefing(None, winner={"source_module": "infrastructure", "content": "prod db down",
                                                "salience": 0.15, "context": {"alarm_lane": True}})
    assert "ALARM-LANE CRITICAL" in text


# --- idle counter survives a restart ----------------------------------------------------

def test_reflecting_cadence_survives_a_daemon_restart(engine, monkeypatch):
    monkeypatch.setattr(runner_mod, "REFLECT_EVERY_N_IDLE", 3)
    first = _runner(extra_scanners={})
    monkeypatch.setattr(first.workspace, "run_cycle", lambda ctx: None)   # ambient modules bid otherwise
    first.run_once(); first.run_once()
    assert json.loads(state_mod.STATE_FILE.read_text(encoding="utf-8"))["idle_cycles"] == 2
    restarted = _runner(extra_scanners={})            # a new process
    monkeypatch.setattr(restarted.workspace, "run_cycle", lambda ctx: None)
    assert restarted._idle_cycles == 2
    assert restarted.run_once()["state"] == "REFLECTING"


# --- quiet hours payload -----------------------------------------------------------------

def test_quiet_hours_deferred_journals_a_compacted_winner(engine, monkeypatch):
    big = [{"type": "local_service_down", "service": "x", "detail": "d" * 20_000, "urgency": 0.5}]
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [_harness("a"), _harness("b")])
    monkeypatch.setattr(command_actuator, "load_quiet_hours", lambda *a, **kw: {"start": "00:00", "end": "23:59"})
    r = _runner(extra_scanners={"scan_big": lambda: big})
    r.run_once(now=datetime(2026, 1, 1, 12, 0))
    deferred = _events("quiet_hours_deferred")
    assert len(deferred) == 2
    assert all(len(json.dumps(e)) < 4000 for e in deferred)


# --- value gate really is off ---------------------------------------------------------------

def test_score_action_is_skipped_while_the_value_gate_is_off(engine, monkeypatch):
    calls = []
    monkeypatch.setattr(runner_mod, "score_action", lambda **kw: calls.append(kw))
    monkeypatch.setattr(runner_mod, "check_work_claim", lambda *a, **kw: {"reason": "no_work_claim"})
    r = _runner()
    real = [{"harness": "h", "ok": True, "dry_run": False, "stdout": "done"}]
    r._post_action_integrity({"source_module": "m", "content": "c", "context": {}}, real)
    assert calls == []
    monkeypatch.setenv("ZUGAMIND_VALUE_GATE_ENABLED", "true")
    r._post_action_integrity({"source_module": "m", "content": "c", "context": {}}, real)
    assert len(calls) == 1


# --- per-harness isolation ---------------------------------------------------------------------

def test_a_raising_harness_does_not_stop_the_next_one_or_the_last_wake_update(engine, monkeypatch):
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [_harness("a"), _harness("b")])
    monkeypatch.setattr(runner_mod, "escalate_for_action", lambda intent, dry_run=False: {"ok": True})
    monkeypatch.setattr(journal, "build_briefing", lambda *a, **kw: "briefing")

    def invoke(hc, briefing, dry_run=False):
        if hc["name"] == "a":
            raise RuntimeError("boom")
        return {"harness": hc["name"], "ok": True, "dry_run": True}
    monkeypatch.setattr(command_actuator, "invoke_harness", invoke)
    result = _runner().run_once()
    names = [(hr["harness"], hr["ok"]) for hr in result["harness_results"]]
    assert names == [("a", False), ("b", True)]
    assert result["harness_results"][0]["error"].startswith("runner_error:")
    assert json.loads(state_mod.STATE_FILE.read_text(encoding="utf-8"))["last_wake"]
    assert not _events("harness_skip")


# --- future last_wake ----------------------------------------------------------------------------

def test_a_future_last_wake_is_journaled_and_the_briefing_window_dropped(engine, monkeypatch):
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [_harness("a")])
    monkeypatch.setattr(runner_mod, "escalate_for_action", lambda intent, dry_run=False: {"ok": True})
    monkeypatch.setattr(command_actuator, "invoke_harness",
                        lambda hc, b, dry_run=False: {"harness": hc["name"], "ok": True, "dry_run": True})
    seen = {}
    monkeypatch.setattr(journal, "build_briefing", lambda since, **kw: seen.setdefault("since", since) or "b")
    st = state_mod.load_state()
    st["last_wake"] = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    state_mod.save_state(st)
    _runner().run_once()
    assert seen["since"] is None
    assert len(_events("last_wake_in_future")) == 1


# --- wake tier typo ----------------------------------------------------------------------------------

def test_a_wake_tier_typo_refuses_to_wake_instead_of_paying_for_haiku(engine, monkeypatch):
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [_harness("a")])
    monkeypatch.setenv("ZUGAMIND_WAKE_TIER", "locl")
    gate_calls = []
    monkeypatch.setattr(runner_mod, "escalate_for_action", lambda intent, dry_run=False: gate_calls.append(intent) or {"ok": True})
    monkeypatch.setattr(journal, "build_briefing", lambda *a, **kw: "b")
    result = _runner().run_once()
    assert result["harness_results"] == [] and gate_calls == []
    skips = _events("harness_skip")
    assert skips and skips[-1]["reason"] == "wake_tier_invalid:locl"


# --- the filter fails closed and explains itself -----------------------------------------------------

def test_filter_reason_fails_closed_on_floors_it_cannot_read():
    w = {"source_module": "m", "content": "c", "salience": 0.9, "context": {}}
    assert StreamRunner._harness_filter_reason({"wake_min_salience": True}, w).startswith("floor_invalid")
    assert StreamRunner._harness_filter_reason({"wake_min_salience": "Calibrate"}, w).startswith("floor_invalid")
    assert StreamRunner._harness_wants({"wake_min_salience": "0.6"}, w) is False


def test_filter_reason_names_the_module_filter_and_the_floor():
    w = {"source_module": "knowledge", "content": "c", "salience": 0.4, "context": {}}
    assert StreamRunner._harness_filter_reason({"wake_modules": ["repo_issues"]}, w).startswith("module_filter:knowledge")
    assert StreamRunner._harness_filter_reason({"wake_min_salience": 0.6}, w) == "floor:modulated 0.400 < 0.600"
    assert StreamRunner._harness_filter_reason({"wake_min_salience": 0.3}, w) is None
    assert StreamRunner._harness_filter_reason({}, w) is None


def test_wake_filtered_event_says_which_harness_refused_and_why(engine, monkeypatch):
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [
        _harness("only-knowledge", wake_modules=["knowledge"]), _harness("high-bar", wake_min_salience=0.99)])
    _runner().run_once()
    ev = _events("wake_filtered")[-1]
    reasons = {f["harness"]: f["reason"] for f in ev["filters"]}
    assert reasons["only-knowledge"].startswith("module_filter:")
    assert reasons["high-bar"].startswith("floor:")


# --- alarm_lane is the lane's to set -----------------------------------------------------------------

def test_a_module_cannot_forge_alarm_lane():
    ws = Workspace()
    forged = SalienceBid(source_module="m", content="c", salience=0.3, thought_type="observation",
                         context={"alarm_lane": True, "triggers": [{"type": "x", "urgency": 0.1}]})
    chosen = ws._select_winner([forged])
    assert chosen is forged and "alarm_lane" not in chosen.context


# --- perception seams ----------------------------------------------------------------------------------

def test_trigger_identity_uses_link_for_ai_labs_shaped_triggers():
    a = scanners_pkg._trigger_key({"type": "ai_lab_research", "link": "https://x/1", "detail": "same text"})
    b = scanners_pkg._trigger_key({"type": "ai_lab_research", "link": "https://x/2", "detail": "same text"})
    assert a != b and a.endswith("https://x/1")


def test_broken_dynamic_scanner_is_logged_not_swallowed(monkeypatch, caplog):
    real_import = scanners_pkg._importlib.import_module

    def broken(name, *a, **kw):
        if name.startswith("scanners.") and name != "scanners":
            raise SyntaxError("bad scanner")
        return real_import(name, *a, **kw)
    monkeypatch.setattr(scanners_pkg._importlib, "import_module", broken)
    with caplog.at_level(logging.WARNING, logger="zugamind.scanners"):
        scanners_pkg.discover_dynamic_scanners()
    assert any("failed to import" in rec.getMessage() for rec in caplog.records)


def test_cycle_event_carries_the_raw_count_next_to_the_habituated_one(engine, monkeypatch):
    r = _runner()
    r._habituated.add("scan_toy")                      # treat the toy as a default world scanner
    monkeypatch.setattr(runner_mod, "habituation_filter", lambda found, **kw: [])
    result = r.run_once()
    ev = _events("cycle")[-1]
    assert (ev["raw_trigger_count"], ev["trigger_count"]) == (1, 0)
    assert result["raw_trigger_count"] == 1


def test_malformed_quiet_hours_warn_once_and_never_suppress(caplog):
    runner_mod._QUIET_WARNED.clear()
    bad = {"start": "23:00:00", "end": "07:00:00"}
    with caplog.at_level(logging.WARNING, logger="zugamind.stream.runner"):
        assert runner_mod.is_quiet_hours(bad, datetime(2026, 1, 1, 2, 0)) is False
        assert runner_mod.is_quiet_hours(bad, datetime(2026, 1, 1, 3, 0)) is False
    assert sum("malformed" in r.getMessage() for r in caplog.records) == 1


# --- work_claim looks at the right repo -----------------------------------------------------------------

def test_work_claim_repo_resolution_order(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit"; (explicit / ".git").mkdir(parents=True)
    env_repo = tmp_path / "env"; (env_repo / ".git").mkdir(parents=True)
    monkeypatch.delenv("ZUGAMIND_WORK_CLAIM_REPO", raising=False)
    assert work_claim._resolve_repo_root(str(explicit)) == str(explicit)
    monkeypatch.setenv("ZUGAMIND_WORK_CLAIM_REPO", str(env_repo))
    assert work_claim._resolve_repo_root(None) == str(env_repo)
    assert work_claim._resolve_repo_root(str(tmp_path / "not-a-repo")) == str(env_repo)
    monkeypatch.delenv("ZUGAMIND_WORK_CLAIM_REPO")
    assert work_claim._resolve_repo_root(None) == work_claim._repo_root()


def test_loader_passes_work_claim_repo_through(tmp_path):
    p = tmp_path / "harness.json"
    p.write_text(json.dumps([{"name": "h", "command": ["h"], "work_claim_repo": " /srv/app "}]), encoding="utf-8")
    cfg = command_actuator.load_harness_configs(p)[0]
    assert cfg["work_claim_repo"] == "/srv/app"


def test_post_action_integrity_hands_the_harness_repo_to_work_claim(engine, monkeypatch):
    seen = {}

    def fake_claim(text, repo_root=None, **kw):
        seen["repo"] = repo_root
        return {"reason": "no_work_claim"}
    monkeypatch.setattr(runner_mod, "check_work_claim", fake_claim)
    r = _runner()
    r._post_action_integrity({"source_module": "m", "content": "c", "context": {}},
                             [{"harness": "h", "ok": True, "dry_run": False, "stdout": "I fixed it"}],
                             [_harness("h", work_claim_repo="/srv/app")])
    assert seen["repo"] == "/srv/app"
