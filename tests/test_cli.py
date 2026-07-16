"""Tests for zugamind.cli — the `zugamind` console-script entry point.

Process spawning/detachment is exercised manually (see cli.py's own
docstring for the walkthrough), not here — spawning real background
processes in a test suite is slow and flaky across CI platforms. These
tests cover what's safe and meaningful to check automatically: PID
liveness checking, real argument parsing via main(), and a real
(read-only) status call against an empty data dir.
"""
import argparse
import os

import pytest

import cli as zugamind_cli


def test_pid_alive_for_current_process():
    assert zugamind_cli._pid_alive(os.getpid()) is True


def test_pid_alive_false_for_bogus_pid():
    # A PID far outside any plausible live range on any platform.
    assert zugamind_cli._pid_alive(999_999_999) is False


def test_main_dispatches_status_to_cmd_status(monkeypatch):
    called = {}

    def fake_status(args):
        called["ran"] = True
        return 0

    monkeypatch.setattr(zugamind_cli, "cmd_status", fake_status)
    rc = zugamind_cli.main(["status"])
    assert rc == 0
    assert called.get("ran") is True


def test_main_with_no_args_dispatches_to_default(monkeypatch):
    called = {}
    monkeypatch.setattr(zugamind_cli, "cmd_default", lambda args: called.setdefault("ran", True) or 0)
    zugamind_cli.main([])
    assert called.get("ran") is True


def test_unknown_subcommand_raises_systemexit():
    with pytest.raises(SystemExit):
        zugamind_cli.main(["not-a-real-command"])


def test_status_on_empty_data_dir_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("ZUGAMIND_DATA_DIR", str(tmp_path))
    import importlib

    from foundation import config as foundation_config
    importlib.reload(foundation_config)
    importlib.reload(zugamind_cli)

    rc = zugamind_cli.cmd_status(argparse.Namespace())
    assert rc == 0
