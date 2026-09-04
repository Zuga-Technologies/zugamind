"""A cycle is recorded in state.json — even when it wakes nothing (2026-09-04).

`last_cycle` and `cycles_today` were declared in `foundation.state._fresh()` and
written by nothing at all. Every status view that read them reported a mind that
had never had a thought, while `journal.jsonl` recorded 150-200 cycles a day.

The second test here is the one that matters. Roughly 95% of cycles wake no
harness (they end in `wake_filtered`), so recording the cycle next to
`last_wake` would have reproduced exactly the same silence in a new place.
"""
from __future__ import annotations

import json

import act.command_actuator as command_actuator
import continuity.journal as journal
import foundation.state as state_mod
from stream.runner import StreamRunner


def _toy_scanner():
    return [{"type": "local_service_down", "service": "toy", "port": 1, "detail": "toy down"}]


def _patch(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    monkeypatch.setattr(state_mod, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(state_mod, "ENGINE_DIR", tmp_path)
    monkeypatch.setattr(command_actuator, "load_quiet_hours", lambda *a, **kw: None)
    # No harness configured: nothing can wake, which is the normal case.
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [])


def _runner():
    return StreamRunner(extra_scanners={"scan_toy": _toy_scanner}, dry_run=True,
                        include_default_scanners=False)


def _state(tmp_path):
    return json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))


def test_a_cycle_that_wakes_nothing_is_still_recorded(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    _runner().run_once()

    saved = _state(tmp_path)
    assert saved["last_cycle"] is not None, "the mind thought and did not write it down"
    assert saved["cycles_today"] == 1
    # Nothing woke. That is precisely why last_cycle cannot live beside last_wake.
    assert saved.get("last_wake") is None


def test_cycles_today_counts_up_within_a_day(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    runner = _runner()
    for _ in range(3):
        runner.run_once()

    assert _state(tmp_path)["cycles_today"] == 3


def test_cycles_today_resets_when_the_date_rolls_over(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    clock = {"iso": "2026-09-04T23:59:00+00:00"}
    monkeypatch.setattr(journal, "now_iso", lambda: clock["iso"])

    runner = _runner()
    runner.run_once()
    runner.run_once()
    assert _state(tmp_path)["cycles_today"] == 2

    clock["iso"] = "2026-09-05T00:01:00+00:00"
    runner.run_once()

    saved = _state(tmp_path)
    assert saved["cycles_today"] == 1, "a new day starts the count over, it does not continue"
    assert saved["last_cycle"].startswith("2026-09-05")


def test_the_count_survives_a_restart(tmp_path, monkeypatch):
    """State is reloaded from disk, so a restart must not zero the day's count."""
    _patch(tmp_path, monkeypatch)
    clock = {"iso": "2026-09-04T10:00:00+00:00"}
    monkeypatch.setattr(journal, "now_iso", lambda: clock["iso"])

    _runner().run_once()
    _runner().run_once()  # a "restart" — fresh runner, same state file

    assert _state(tmp_path)["cycles_today"] == 2
