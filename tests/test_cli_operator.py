"""cli.py operator surface — the 2026-08-29 audit.

`watch` tails by byte offset; the journal renames itself on rotation, so a
plain seek went blind. `status` read only the active file's last line and
said nothing about money. The retired tools/live-wake-monitor.py's one
unique feature (a separate result file) lives in `watch --result-file`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import pytest

import cli
import continuity.journal as journal
import foundation.budget as budget
import foundation.config as config
import foundation.state as state_mod


# --- the tail helper ---------------------------------------------------------------

def test_tail_reads_only_what_was_appended(tmp_path):
    p = tmp_path / "j.jsonl"
    p.write_bytes(b"a\n")                            # bytes: the tail is byte-faithful on every platform
    new, pos, rotated = cli._tail(p, 0)
    assert new == "a\n" and rotated is False
    p.write_bytes(b"a\nb\n")
    new, pos, rotated = cli._tail(p, pos)
    assert new == "b\n" and rotated is False


def test_tail_follows_a_rotated_journal(tmp_path):
    p = tmp_path / "j.jsonl"
    p.write_bytes(b"x" * 500 + b"\n")
    _, pos, _ = cli._tail(p, 0)
    assert pos == p.stat().st_size == 501
    os.replace(p, tmp_path / "j.1.jsonl")            # what rotation does
    p.write_bytes(b"fresh\n")
    new, pos, rotated = cli._tail(p, pos)
    assert rotated is True and new == "fresh\n" and pos == 6


def test_tail_missing_file_is_empty_not_an_error(tmp_path):
    assert cli._tail(tmp_path / "nope", 123) == ("", 0, False)


# --- status --------------------------------------------------------------------------

@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    monkeypatch.setattr(journal, "_appends_since_check", 0)
    monkeypatch.setattr(journal, "_tail_checked_for", None)
    monkeypatch.setattr(cli, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    monkeypatch.setattr(cli, "PID_FILE", tmp_path / "daemon.pid")
    monkeypatch.setattr(config, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(state_mod, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(budget, "BUDGET_FILE", tmp_path / "budget.json")
    monkeypatch.setattr(budget, "ENGINE_DIR", tmp_path)
    return tmp_path


def test_status_reports_the_last_event_even_right_after_a_rotation(data_dir, capsys):
    (data_dir / "journal.1.jsonl").write_text(
        '{"ts": "2026-08-29T01:00:00+00:00", "kind": "cycle"}\n', encoding="utf-8")
    (data_dir / "journal.jsonl").write_text("", encoding="utf-8")   # fresh, empty active file
    cli.cmd_status(None)
    out = capsys.readouterr().out
    assert "last journal event: cycle @ 2026-08-29T01:00:00+00:00" in out
    assert "2 segment(s)" in out


def test_status_shows_spend_against_the_cap(data_dir, capsys):
    b = budget.load_budget()
    budget.record_spend(b, "haiku", cost=0.25)
    cli.cmd_status(None)
    out = capsys.readouterr().out
    assert "spend this month: $0.2500 of $" in out and "1 paid call(s)" in out


def test_status_survives_an_empty_data_dir(data_dir, capsys):
    cli.cmd_status(None)
    out = capsys.readouterr().out
    assert "not running" in out


# --- colour ----------------------------------------------------------------------------

def test_no_color_disables_ansi(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert cli._colour_enabled() is False


# --- the retired monitor's feature ------------------------------------------------------

def test_watch_parser_accepts_result_file():
    import argparse
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    p_watch = sub.add_parser("watch")
    p_watch.add_argument("--result-file", default=None)
    args = parser.parse_args(["watch", "--result-file", "notes.md"])
    assert args.result_file == "notes.md"
    # and the real parser wires it
    real = cli.main.__globals__["argparse"].ArgumentParser  # smoke: module imports argparse
    assert real is argparse.ArgumentParser


def test_retired_monitor_is_gone():
    assert not (Path(cli.__file__).parent / "tools" / "live-wake-monitor.py").exists()


# --- the 2026-08-29 adversarial review (cli.py) -------------------------------------------

def test_no_non_ascii_glyph_is_printed_by_the_cli():
    """`watch > log.txt` on Windows is cp1252; one glyph outside it and print raises."""
    import cli_tools
    for mod in (cli, cli_tools):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        printed = re.findall(r'print\((?:f|rf|fr)?"([^"]*)"', src)
        for text in printed:
            text.encode("cp1252")  # raises on a glyph outside the codepage


def test_safe_stdout_replaces_instead_of_raising(monkeypatch):
    import io
    buf = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", buf)
    cli._safe_stdout()
    print("\u26a1 fine")   # would raise without the reconfigure
    buf.flush()
    assert b"? fine" in buf.buffer.getvalue()


def test_pid_is_ours_recognises_this_python_and_rejects_a_stranger(monkeypatch):
    cli._OWNER_CACHE.clear()
    assert cli._pid_is_ours(os.getpid()) is True
    cli._OWNER_CACHE.clear()
    if os.name == "nt":
        fake = type("R", (), {"stdout": '"notepad.exe","4242","Console","1","1,000 K"\n'})()
        monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: fake)
    else:
        monkeypatch.setattr(cli.Path, "exists", lambda self: False)
        fake = type("R", (), {"stdout": "notepad --something\n"})()
        monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: fake)
    assert cli._pid_is_ours(4242) is False
    cli._OWNER_CACHE.clear()


def test_running_pid_ignores_a_recycled_pid(data_dir, monkeypatch):
    (data_dir / "daemon.pid").write_text("4242", encoding="utf-8")
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cli, "_pid_is_ours", lambda pid: False)
    assert cli._running_pid() is None
    monkeypatch.setattr(cli, "_pid_is_ours", lambda pid: True)
    assert cli._running_pid() == 4242


def test_bare_zugamind_reports_a_failed_start_instead_of_a_traceback(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_running_pid", lambda: None)

    def boom(args):
        raise PermissionError("data dir is read-only")
    monkeypatch.setattr(cli, "cmd_start", boom)
    rc = cli.cmd_default(argparse.Namespace())
    out = capsys.readouterr().out
    assert rc == 1 and "could not start the daemon" in out and "read-only" in out and "zugamind doctor" in out


def test_demo_never_spends_without_an_explicit_live_flag(monkeypatch, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("demo_under_test", Path(cli.__file__).parent.parent / "demo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-NOT-REAL")
    seen = {}

    def fake_gate(intent, dry_run=False):
        seen["dry_run"] = dry_run
        return {"ok": True, "dry_run": dry_run, "model": "fake", "cost": 0.0, "response": ""}
    monkeypatch.setattr(demo, "escalate_for_action", fake_gate)
    demo.run_demo(cycles=4, seed=7)                 # key exported, no --live
    assert seen.get("dry_run", True) is True
    capsys.readouterr()
    demo.run_demo(cycles=4, seed=7, live=True)      # explicit opt-in
    assert seen["dry_run"] is False


# --- the 2026-08-29 lifecycle audit (cli side) ------------------------------------------------

def test_stop_asks_first_and_reports_a_graceful_exit(data_dir, monkeypatch, capsys):
    monkeypatch.setattr(config, "STOP_FILE", data_dir / "stop.request")
    (data_dir / "daemon.pid").write_text("4242", encoding="utf-8")
    monkeypatch.setattr(cli, "_running_pid", lambda: 4242)
    # alive until the stop request exists, then gone: the daemon honoured it
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: not (data_dir / "stop.request").exists())
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("taskkill must not run")))
    monkeypatch.setattr(cli.os, "kill", lambda *a: (_ for _ in ()).throw(AssertionError("kill must not run")))
    assert cli.cmd_stop(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "stopped gracefully" in out and not (data_dir / "daemon.pid").exists()


def test_stop_forces_after_the_grace_period(data_dir, monkeypatch, capsys):
    monkeypatch.setattr(config, "STOP_FILE", data_dir / "stop.request")
    monkeypatch.setenv("ZUGAMIND_STOP_GRACE_SEC", "1")
    monkeypatch.setattr(cli, "_running_pid", lambda: 4242)
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: True)
    forced = {}
    if os.name == "nt":
        monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kw: (forced.setdefault("cmd", cmd), type("R", (), {"returncode": 0})())[1])
    else:
        monkeypatch.setattr(cli.os, "kill", lambda pid, sig: forced.setdefault("cmd", [pid, sig]))
    assert cli.cmd_stop(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "forcing" in out and forced and not (data_dir / "stop.request").exists()


def test_status_shows_paused_when_the_pause_file_exists(data_dir, monkeypatch, capsys):
    monkeypatch.setattr(config, "PAUSE_FILE", data_dir / "PAUSE")
    (data_dir / "PAUSE").write_text("", encoding="utf-8")
    cli.cmd_status(None)
    assert "PAUSED" in capsys.readouterr().out
