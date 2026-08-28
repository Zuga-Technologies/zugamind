"""act/command_actuator.py — the 2026-08-28 audit gaps.

The loader's fail-closed rejections, the never-raises contract on hand-built
dicts, dry runs not consuming quota, UTF-8 harness output, and the timeout
that must kill the WHOLE process tree (the .cmd shim's grandchild used to
survive, keep acting, and hold the stdout pipe).
"""
from __future__ import annotations

import json
import logging
import sys
import time

import pytest

import act.command_actuator as command_actuator
import continuity.journal as journal


def _patch_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")


def _config(**overrides):
    base = {"name": "t", "command": [sys.executable, "-c", "print('ok')"],
            "timeout_sec": 10, "max_per_hour": 4, "enabled": True}
    base.update(overrides)
    return base


def _write_configs(tmp_path, entries):
    p = tmp_path / "harness.json"
    p.write_text(json.dumps(entries), encoding="utf-8")
    return p


GOOD = {"name": "good", "command": ["echo"]}


# --- loader: fail closed on anything it cannot read correctly ---------------

@pytest.mark.parametrize("bad", [
    {"name": "x", "command": ["y"], "timeout_sec": "soon"},
    {"name": "x", "command": ["y"], "max_per_hour": None},
    {"name": "x", "command": ["y"], "max_per_day": "lots"},
    {"name": "x", "command": ["y"], "enabled": "nonsense"},
    {"name": "x", "command": ["y"], "wake_min_salience": "calibrated"},   # the typo
    {"name": "x", "command": ["y"], "wake_min_salience": "0.6"},          # a string number
    {"name": "x", "command": ["y"], "wake_min_salience": True},
])
def test_malformed_entry_is_skipped_never_raised_and_never_half_loaded(tmp_path, bad, caplog):
    path = _write_configs(tmp_path, [bad, GOOD])
    with caplog.at_level(logging.WARNING):
        cfgs = command_actuator.load_harness_configs(path)
    assert [c["name"] for c in cfgs] == ["good"]
    assert any("skipping harness 'x'" in r.message for r in caplog.records)


@pytest.mark.parametrize("value,expected", [
    (False, False), ("false", False), ("False", False), ("0", False), ("no", False), ("off", False),
    (True, True), ("true", True), ("1", True), ("yes", True), (1, True),
])
def test_enabled_accepts_only_boolean_spellings(tmp_path, value, expected):
    """bool("false") is True — the string once silently ENABLED a harness."""
    cfgs = command_actuator.load_harness_configs(
        _write_configs(tmp_path, [{"name": "x", "command": ["y"], "enabled": value}]))
    assert cfgs[0]["enabled"] is expected


def test_duplicate_names_keep_the_first_entry_only(tmp_path, caplog):
    path = _write_configs(tmp_path, [
        {"name": "dup", "command": ["first"]}, {"name": "dup", "command": ["second"]}, GOOD])
    with caplog.at_level(logging.WARNING):
        cfgs = command_actuator.load_harness_configs(path)
    assert [(c["name"], c["command"]) for c in cfgs] == [("dup", ["first"]), ("good", ["echo"])]
    assert any("duplicate harness name 'dup'" in r.message for r in caplog.records)


def test_calibrate_and_numeric_floors_still_load(tmp_path):
    cfgs = command_actuator.load_harness_configs(_write_configs(tmp_path, [
        {"name": "a", "command": ["y"], "wake_min_salience": "calibrate"},
        {"name": "b", "command": ["y"], "wake_min_salience": 0.6},
        {"name": "c", "command": ["y"]},
    ]))
    assert [c.get("wake_min_salience") for c in cfgs] == ["calibrate", 0.6, None]


# --- invoke_harness never raises on a hand-built dict ------------------------

def test_bad_caps_on_a_hand_built_config_fall_back_to_defaults(tmp_path, monkeypatch, caplog):
    _patch_journal(tmp_path, monkeypatch)
    cfg = _config(max_per_hour="nope", max_per_day=None, timeout_sec="soon")
    with caplog.at_level(logging.WARNING):
        result = command_actuator.invoke_harness(cfg, "b", dry_run=True)
    assert result["ok"] is True
    assert sum("is not an integer; using default" in r.message for r in caplog.records) >= 2


# --- quota: dry runs and empty commands do not consume it ------------------

def test_dry_runs_do_not_consume_the_quota(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    cfg = _config(name="q", max_per_hour=1)
    for _ in range(3):
        assert command_actuator.invoke_harness(cfg, "b", dry_run=True)["ok"] is True
    real = command_actuator.invoke_harness(cfg, "b", dry_run=False)
    assert real["ok"] is True, real
    assert command_actuator.invoke_harness(cfg, "b", dry_run=False)["error"] == "rate_limited"


def test_empty_command_does_not_consume_the_quota(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    for _ in range(3):
        assert command_actuator.invoke_harness(_config(name="q", max_per_hour=1, command=[]), "b")["error"] == "empty_command"
    assert command_actuator.invoke_harness(_config(name="q", max_per_hour=1), "b", dry_run=False)["ok"] is True


def test_hour_and_day_windows_come_from_one_read(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(journal, "now_iso", lambda: "2026-01-01T00:00:00+00:00")
    journal.append_event("harness_invocation", {"harness": "w", "ok": True})            # 5h ago: day only
    monkeypatch.setattr(journal, "now_iso", lambda: "2026-01-01T04:30:00+00:00")
    journal.append_event("harness_invocation", {"harness": "w", "ok": True})            # 30m ago: both
    journal.append_event("harness_invocation", {"harness": "w", "ok": True, "dry_run": True})  # never
    monkeypatch.setattr(journal, "now_iso", lambda: "2026-01-01T05:00:00+00:00")
    from datetime import datetime, timezone
    now = datetime(2026, 1, 1, 5, 0, tzinfo=timezone.utc).timestamp()
    assert command_actuator._recent_invocation_counts("w", now=now) == (1, 2)


# --- output decoding ---------------------------------------------------------

def test_utf8_harness_output_survives_intact(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    script = tmp_path / "emit.py"
    script.write_text("import sys; sys.stdout.buffer.write('done \\U0001F34E \\u2014 ok\\n'.encode('utf-8'))",
                      encoding="utf-8")
    result = command_actuator.invoke_harness(_config(command=[sys.executable, str(script)]), "b", dry_run=False)
    assert result["ok"] is True, result
    assert "\U0001F34E" in result["stdout"] and "—" in result["stdout"]


# --- timeout kills the whole tree --------------------------------------------

def test_timeout_kills_the_grandchild_and_does_not_block_on_its_pipe(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    marker = tmp_path / "grandchild_alive.txt"
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(
        f"import time, pathlib\ntime.sleep(4)\npathlib.Path({str(marker)!r}).write_text('alive')\n",
        encoding="utf-8")
    parent = tmp_path / "parent.py"
    parent.write_text(
        f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, {str(grandchild)!r}])\n"
        "print('parent started', flush=True)\ntime.sleep(30)\n", encoding="utf-8")
    argv = [sys.executable, str(parent)]
    if sys.platform == "win32":
        argv = ["cmd.exe", "/c", *argv]  # the exact shim shape used for .cmd harnesses

    t0 = time.time()
    result = command_actuator.invoke_harness(_config(command=argv, timeout_sec=1), "b", dry_run=False)
    elapsed = time.time() - t0

    assert result["error"] == "timeout" and result["killed_tree"] is True
    assert "parent started" in result["stdout"]           # partial output salvaged
    assert elapsed < 1 + command_actuator._TREE_KILL_GRACE_SEC + 5
    time.sleep(5)
    assert not marker.exists(), "the grandchild survived the timeout kill"


# --- briefing cleanup ---------------------------------------------------------

def test_briefing_cleanup_failure_is_logged_not_swallowed(tmp_path, monkeypatch, caplog):
    _patch_journal(tmp_path, monkeypatch)
    real_unlink = command_actuator.os.unlink

    def refuse(path):
        raise OSError("in use")
    monkeypatch.setattr(command_actuator.os, "unlink", refuse)
    with caplog.at_level(logging.WARNING):
        result = command_actuator.invoke_harness(_config(), "b", dry_run=True)
    monkeypatch.setattr(command_actuator.os, "unlink", real_unlink)
    assert result["ok"] is True
    assert any("briefing file not removed" in r.message for r in caplog.records)
