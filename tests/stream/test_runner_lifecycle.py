"""stream/runner.py daemon lifecycle — the 2026-08-29 audit.

Proved on this box: on Windows `taskkill /F` and `os.kill(pid, SIGTERM)`
are TerminateProcess — the daemon's signal handler never ran, so no
"shutdown" event was ever journaled by an external stop; perception ran
outside run_once's guard; `save_state` raised PermissionError out of a
cycle under two-process contention; `--cycles 0` ran one cycle and every
run exited 0; nothing emitted `daemon_restarted`.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

import act.command_actuator as command_actuator
import act.floor_calibration as floor_calibration
import continuity.journal as journal
import foundation.config as config
import foundation.fs as fs
import foundation.state as state_mod
import stream.runner as runner_mod
from stream.runner import StreamRunner


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    monkeypatch.setattr(journal, "_appends_since_check", 0)
    monkeypatch.setattr(journal, "_tail_checked_for", None)
    monkeypatch.setattr(state_mod, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(state_mod, "ENGINE_DIR", tmp_path)
    monkeypatch.setattr(config, "PAUSE_FILE", tmp_path / "PAUSE")
    monkeypatch.setattr(config, "STOP_FILE", tmp_path / "stop.request")
    monkeypatch.setattr(floor_calibration, "STATE_FILE", tmp_path / "floor.json")
    monkeypatch.setattr(command_actuator, "load_quiet_hours", lambda *a, **kw: None)
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [])
    return tmp_path


def _runner(**kw):
    kw.setdefault("extra_scanners", {"scan_toy": lambda: [{"type": "local_service_down", "service": "s", "detail": "d"}]})
    kw.setdefault("include_default_scanners", False)
    kw.setdefault("dry_run", True)
    return StreamRunner(**kw)


def _events(kind):
    return [e for e in journal.read_events(limit=None) if e.get("kind") == kind]


# --- perception is guarded --------------------------------------------------------------------

def test_a_raise_in_the_router_is_a_cycle_error_line_not_a_dead_cycle(engine, monkeypatch):
    monkeypatch.setattr(runner_mod, "route_triggers_to_modules", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("router")))
    result = _runner().run_once()
    assert result["cycle_error"] == "router"
    errs = _events("cycle_error")
    assert errs and errs[-1]["phase"] == "perception"
    assert _events("cycle")                       # the cycle still got journaled


def test_a_raise_in_the_scheduler_is_guarded_too(engine, monkeypatch):
    r = _runner()
    monkeypatch.setattr(r.scheduler, "start_cycle", lambda: (_ for _ in ()).throw(RuntimeError("sched")))
    assert r.run_once()["cycle_error"] == "sched"


# --- state persistence never kills a cycle --------------------------------------------------------

def test_a_failed_state_save_is_journaled_and_the_cycle_completes(engine, monkeypatch):
    monkeypatch.setattr(runner_mod, "save_state", lambda s: (_ for _ in ()).throw(PermissionError("locked")))
    result = _runner().run_once()
    assert result["winner"] is not None
    assert _events("state_persist_failed")


def test_atomic_write_retries_the_windows_replace_race(tmp_path, monkeypatch):
    real = os.replace
    attempts = {"n": 0}

    def flaky(src, dst):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise PermissionError("in flight")
        return real(src, dst)
    monkeypatch.setattr(fs.os, "replace", flaky)
    monkeypatch.setattr(fs, "_REPLACE_RETRY_SLEEP_S", 0)
    fs.atomic_write_text(tmp_path / "s.json", "{}")
    assert (tmp_path / "s.json").read_text(encoding="utf-8") == "{}" and attempts["n"] == 3


def test_atomic_write_gives_up_after_the_retry_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(fs.os, "replace", lambda s, d: (_ for _ in ()).throw(PermissionError("stuck")))
    monkeypatch.setattr(fs, "_REPLACE_RETRY_SLEEP_S", 0)
    with pytest.raises(PermissionError):
        fs.atomic_write_text(tmp_path / "s.json", "{}")
    assert not list(tmp_path.glob("*.tmp"))          # no litter


# --- main(): honest exit codes ----------------------------------------------------------------------

class _FakeRunner:
    def __init__(self, results):
        self._results = results

    def run_cycles(self, n):
        return self._results[:n] if n else []


def test_main_exit_code_reflects_cycle_errors(monkeypatch, capsys):
    ok = {"winner": None, "state": "RESTING", "trigger_count": 0, "harness_results": [], "cycle_error": None}
    bad = dict(ok, cycle_error="boom")
    monkeypatch.setattr(runner_mod, "StreamRunner", lambda **kw: _FakeRunner([ok, bad]))
    assert runner_mod.main(["--cycles", "2"]) == 1
    assert "cycle error: boom" in capsys.readouterr().out
    monkeypatch.setattr(runner_mod, "StreamRunner", lambda **kw: _FakeRunner([ok, ok]))
    assert runner_mod.main(["--cycles", "2"]) == 0


def test_main_cycles_zero_runs_nothing_and_negative_is_a_usage_error(monkeypatch, capsys):
    monkeypatch.setattr(runner_mod, "StreamRunner", lambda **kw: _FakeRunner([{"cycle_error": "x"}]))
    assert runner_mod.main(["--cycles", "0"]) == 0
    assert "cycle 1/" not in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        runner_mod.main(["--cycles", "-3"])
    assert e.value.code == 2


# --- the daemon loop: cooperative stop + restart detection --------------------------------------------

def test_stop_file_shuts_the_daemon_down_after_the_current_cycle(engine, monkeypatch):
    r = _runner()
    cycles = {"n": 0}
    monkeypatch.setattr(r, "run_once", lambda: cycles.__setitem__("n", cycles["n"] + 1))
    t = threading.Thread(target=r.run_daemon, kwargs={"interval": 1}, daemon=True)
    t.start()
    time.sleep(0.3)
    config.STOP_FILE.write_text("1", encoding="utf-8")
    t.join(timeout=5)
    assert not t.is_alive()
    assert cycles["n"] >= 1
    assert _events("shutdown")[-1]["reason"] == "stop_file"
    assert not config.STOP_FILE.exists()             # consumed


def test_a_stale_stop_request_does_not_stop_a_fresh_daemon(engine, monkeypatch):
    config.STOP_FILE.write_text("old", encoding="utf-8")
    r = _runner()
    seen = {"n": 0}

    def once():
        seen["n"] += 1
        if seen["n"] == 2:
            config.STOP_FILE.write_text("now", encoding="utf-8")
    monkeypatch.setattr(r, "run_once", once)
    t = threading.Thread(target=r.run_daemon, kwargs={"interval": 1}, daemon=True)
    t.start()
    t.join(timeout=8)
    assert not t.is_alive() and seen["n"] == 2       # the stale file was ignored; the fresh one honoured


def test_startup_journals_started_after_a_clean_shutdown_and_restarted_otherwise(engine):
    r = _runner()
    r._journal_startup()
    assert _events("daemon_started") and not _events("daemon_restarted")
    journal.append_event("cycle", {"winner": None})
    r._journal_startup()                              # last event is a cycle: the previous run was killed
    ev = _events("daemon_restarted")[-1]
    assert ev["last_event_kind"] == "cycle" and ev["pid"] == os.getpid()
    journal.append_event("shutdown", {"reason": "stop_file"})
    r._journal_startup()
    assert len(_events("daemon_started")) == 2


def test_a_raise_out_of_run_once_in_the_daemon_loop_is_journaled(engine, monkeypatch):
    r = _runner()
    calls = {"n": 0}

    def once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("loop boom")
        config.STOP_FILE.write_text("1", encoding="utf-8")
    monkeypatch.setattr(r, "run_once", once)
    t = threading.Thread(target=r.run_daemon, kwargs={"interval": 1}, daemon=True)
    t.start()
    t.join(timeout=8)
    assert not t.is_alive()
    errs = _events("cycle_error")
    assert errs and errs[-1]["phase"] == "daemon_loop" and "loop boom" in errs[-1]["error"]


def test_stream_package_exposes_the_runner_lazily():
    import stream
    assert stream.StreamRunner is StreamRunner
    src = Path(stream.__file__).read_text(encoding="utf-8")
    assert "from .runner import StreamRunner\n" not in src.split("def __getattr__")[0]
