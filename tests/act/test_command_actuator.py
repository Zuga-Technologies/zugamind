"""Tests for act/command_actuator.py — the harness adapter.

Covers: config loading + normalization, {briefing_file} placeholder
substitution, dry_run (no exec, still journals), the real subprocess path
(mocked with a real tiny Python one-liner — no external harness needed),
the timeout path, the per-hour AND per-day rate limits (via synthetic
journal entries), quiet-hours config resolution, and the never-raises
contract for a bogus command.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import act.command_actuator as command_actuator
import continuity.journal as journal


def _patch_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")


def _config(**overrides):
    base = {
        "name": "test-harness",
        "command": [sys.executable, "-c", "print('ok')"],
        "timeout_sec": 10,
        "max_per_hour": 4,
        "enabled": True,
    }
    base.update(overrides)
    return base


# --- load_harness_configs -------------------------------------------------

def test_load_harness_configs_missing_file_returns_empty(tmp_path):
    assert command_actuator.load_harness_configs(tmp_path / "nope.json") == []


def test_load_harness_configs_bare_list(tmp_path):
    p = tmp_path / "harness.json"
    p.write_text(json.dumps([_config(name="a"), _config(name="b", enabled=False)]))
    configs = command_actuator.load_harness_configs(p)
    assert [c["name"] for c in configs] == ["a", "b"]
    assert configs[1]["enabled"] is False


def test_load_harness_configs_wrapped_dict_form(tmp_path):
    p = tmp_path / "harness.json"
    p.write_text(json.dumps({"harnesses": [_config(name="a")]}))
    configs = command_actuator.load_harness_configs(p)
    assert len(configs) == 1
    assert configs[0]["timeout_sec"] == 10


def test_load_harness_configs_skips_malformed_entries(tmp_path):
    p = tmp_path / "harness.json"
    p.write_text(json.dumps([{"name": "no-command"}, _config(name="good")]))
    configs = command_actuator.load_harness_configs(p)
    assert [c["name"] for c in configs] == ["good"]


def test_load_harness_configs_corrupt_json_returns_empty(tmp_path):
    p = tmp_path / "harness.json"
    p.write_text("{ not json")
    assert command_actuator.load_harness_configs(p) == []


def test_load_harness_configs_defaults_applied(tmp_path):
    p = tmp_path / "harness.json"
    p.write_text(json.dumps([{"name": "bare", "command": ["true"]}]))
    configs = command_actuator.load_harness_configs(p)
    c = configs[0]
    assert c["timeout_sec"] == 300
    assert c["max_per_hour"] == 4
    assert c["enabled"] is True


# --- invoke_harness: placeholder substitution + dry_run -------------------

def test_dry_run_never_executes_but_journals(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    config = _config(command=["some-harness", "--file", "{briefing_file}"])
    result = command_actuator.invoke_harness(config, "briefing text", dry_run=True)

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["would_run"][0] == "some-harness"
    assert result["would_run"][1] == "--file"
    assert result["would_run"][2] != "{briefing_file}"  # substituted to a real path

    events = journal.read_events()
    assert len(events) == 1
    assert events[0]["kind"] == "harness_invocation"
    assert events[0]["dry_run"] is True


def test_real_invocation_runs_tiny_command_and_captures_output(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    # Placeholder passed as a separate argv element (not embedded in the -c
    # source string) — a Windows temp path contains backslashes that would
    # otherwise be parsed as Python string escapes inside a quoted literal.
    config = _config(command=[sys.executable, "-c",
                              "import sys; print('briefing at', sys.argv[1])",
                              "{briefing_file}"])
    result = command_actuator.invoke_harness(config, "hello briefing", dry_run=False)

    assert result["ok"] is True
    assert result["returncode"] == 0
    assert "briefing at" in result["stdout"]
    assert result["dry_run"] is False

    events = journal.read_events()
    assert events[-1]["kind"] == "harness_invocation"
    assert events[-1]["ok"] is True


def test_timeout_path_returns_ok_false_never_raises(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    config = _config(
        command=[sys.executable, "-c", "import time; time.sleep(5)"],
        timeout_sec=1,
    )
    result = command_actuator.invoke_harness(config, "b", dry_run=False)
    assert result["ok"] is False
    assert result["error"] == "timeout"


def test_bogus_command_never_raises(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    config = _config(command=["this-binary-does-not-exist-zzz", "{briefing_file}"])
    result = command_actuator.invoke_harness(config, "b", dry_run=False)
    assert result["ok"] is False
    assert "error" in result


def test_disabled_harness_refuses_without_running(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    config = _config(enabled=False)
    result = command_actuator.invoke_harness(config, "b", dry_run=False)
    assert result["ok"] is False
    assert result["error"] == "harness_disabled"


def test_empty_command_is_refused(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    result = command_actuator.invoke_harness(_config(command=[]), "b", dry_run=True)
    assert result["ok"] is False
    assert result["error"] == "empty_command"


# --- rate limiting via synthetic journal entries ---------------------------

def test_rate_limit_refuses_after_max_per_hour(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    config = _config(name="rate-test", max_per_hour=2)

    r1 = command_actuator.invoke_harness(config, "b", dry_run=True)
    r2 = command_actuator.invoke_harness(config, "b", dry_run=True)
    assert r1["ok"] and r2["ok"]

    r3 = command_actuator.invoke_harness(config, "b", dry_run=True)
    assert r3["ok"] is False
    assert r3["error"] == "rate_limited"

    events = journal.read_events()
    assert sum(1 for e in events if e["kind"] == "harness_rate_limited") == 1


def test_rate_limit_ignores_invocations_older_than_an_hour(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    config = _config(name="rate-test-2", max_per_hour=1)

    monkeypatch.setattr(journal, "now_iso", lambda: "2026-01-01T00:00:00+00:00")
    journal.append_event("harness_invocation", {"harness": "rate-test-2", "ok": True})

    monkeypatch.setattr(journal, "now_iso", lambda: "2026-01-01T05:00:00+00:00")
    fixed_now = datetime(2026, 1, 1, 5, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(command_actuator.time, "time", lambda: fixed_now)
    result = command_actuator.invoke_harness(config, "b", dry_run=True)
    assert result["ok"] is True


def test_rate_limit_counts_only_matching_harness_name(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    other = _config(name="other-harness", max_per_hour=1)
    mine = _config(name="rate-test-3", max_per_hour=1)

    r_other = command_actuator.invoke_harness(other, "b", dry_run=True)
    assert r_other["ok"] is True

    r_mine = command_actuator.invoke_harness(mine, "b", dry_run=True)
    assert r_mine["ok"] is True  # different harness name — not rate limited by other's count


# --- per-day cap (independent of the per-hour cap) -------------------------

def test_per_day_cap_refuses_after_max_per_day(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    # A generous per-hour cap isolates the per-day cap as the thing that trips.
    config = _config(name="day-cap-test", max_per_hour=100, max_per_day=2)

    r1 = command_actuator.invoke_harness(config, "b", dry_run=True)
    r2 = command_actuator.invoke_harness(config, "b", dry_run=True)
    assert r1["ok"] and r2["ok"]

    r3 = command_actuator.invoke_harness(config, "b", dry_run=True)
    assert r3["ok"] is False
    assert r3["error"] == "rate_limited"
    assert r3["window"] == "day"

    events = journal.read_events()
    day_limited = [e for e in events if e["kind"] == "harness_rate_limited" and e.get("window") == "day"]
    assert len(day_limited) == 1


def test_per_day_cap_ignores_invocations_older_than_24h(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    config = _config(name="day-cap-test-2", max_per_hour=100, max_per_day=1)

    monkeypatch.setattr(journal, "now_iso", lambda: "2026-01-01T00:00:00+00:00")
    journal.append_event("harness_invocation", {"harness": "day-cap-test-2", "ok": True})

    later = datetime(2026, 1, 2, 1, 0, tzinfo=timezone.utc)  # 25h after the event above
    monkeypatch.setattr(journal, "now_iso", lambda: later.isoformat())
    monkeypatch.setattr(command_actuator.time, "time", lambda: later.timestamp())

    result = command_actuator.invoke_harness(config, "b", dry_run=True)
    assert result["ok"] is True  # the 25h-old invocation no longer counts toward the 24h window


def test_load_harness_configs_max_per_day_default_and_override(tmp_path):
    p = tmp_path / "harness.json"
    p.write_text(json.dumps([_config(name="a"), _config(name="b", max_per_day=5)]))
    configs = {c["name"]: c for c in command_actuator.load_harness_configs(p)}
    assert configs["a"]["max_per_day"] == 20
    assert configs["b"]["max_per_day"] == 5


# --- quiet-hours config resolution ------------------------------------------

def test_load_quiet_hours_no_config_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("ZUGAMIND_QUIET_HOURS", raising=False)
    assert command_actuator.load_quiet_hours(tmp_path / "nope.json") is None


def test_load_quiet_hours_from_dict_form_config_file(tmp_path, monkeypatch):
    monkeypatch.delenv("ZUGAMIND_QUIET_HOURS", raising=False)
    p = tmp_path / "harness.json"
    p.write_text(json.dumps({"harnesses": [_config()], "quiet_hours": {"start": "23:00", "end": "07:00"}}))
    quiet = command_actuator.load_quiet_hours(p)
    assert quiet == {"start": "23:00", "end": "07:00"}


def test_load_quiet_hours_bare_list_config_has_no_slot(tmp_path, monkeypatch):
    monkeypatch.delenv("ZUGAMIND_QUIET_HOURS", raising=False)
    p = tmp_path / "harness.json"
    p.write_text(json.dumps([_config()]))
    assert command_actuator.load_quiet_hours(p) is None


def test_load_quiet_hours_env_var_overrides_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ZUGAMIND_QUIET_HOURS", "22:00-06:30")
    p = tmp_path / "harness.json"
    p.write_text(json.dumps({"harnesses": [_config()], "quiet_hours": {"start": "23:00", "end": "07:00"}}))
    quiet = command_actuator.load_quiet_hours(p)
    assert quiet == {"start": "22:00", "end": "06:30"}


def test_load_quiet_hours_malformed_env_falls_back_to_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ZUGAMIND_QUIET_HOURS", "not-a-range")
    p = tmp_path / "harness.json"
    p.write_text(json.dumps({"harnesses": [_config()], "quiet_hours": {"start": "23:00", "end": "07:00"}}))
    quiet = command_actuator.load_quiet_hours(p)
    assert quiet == {"start": "23:00", "end": "07:00"}
