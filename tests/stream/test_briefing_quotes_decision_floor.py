"""The briefing must quote the floor the gate COMPARED, not the one it left behind.

Live shape, 2026-09-05 18:54: the raw-fitted floor was 0.520 (the 18:47
`wake_filtered` event says so). A world_signals bid of raw 0.55 cleared it
and woke a session. The briefing that session read said:

    bar 0.555 fitted on the raw series; judged on min(bid, modulated) = 0.55

— a wake described as failing its own gate. It had not. The runner records
the winner as an ambient calibration sample BEFORE it builds the briefing,
and `_wake_gate_hints` reads the calibrated floor live from the state file,
so the number in the briefing was the floor AFTER this winner's own 0.55
had moved the 90th percentile — a floor no decision had been made against
yet. Any wake high enough to move the quantile (which is what a wake tends
to be) reports itself as unearned, and the woken session spends its first
turn re-deriving the truth from journal.jsonl — the exact cost the note was
added to prevent (2026-08-17).
"""
from __future__ import annotations

import act.command_actuator as command_actuator
import act.floor_calibration as floor_calibration
import continuity.journal as journal
import foundation.state as state_mod
import stream.runner as runner_mod
from stream.runner import StreamRunner

HARNESS = {
    "name": "cc", "command": ["x", "{briefing_file}"],
    "timeout_sec": 10, "max_per_hour": 4, "enabled": True,
    "wake_min_salience": "calibrate",
}


def _ambient(raw):
    return {"source_module": "priority_goals", "salience": raw, "content": "ambient",
            "context": {"raw_salience": raw}}


def _seed_floor_at_0_520():
    """50 raw samples whose nearest-rank 90th percentile is 0.47 -> floor 0.520,
    laid out oldest-first so the eviction on the next sample drops a 0.20 and
    the six values >= 0.47 all survive: appending 0.55 then makes
    sorted[44] = 0.505 -> floor 0.555. Same arithmetic as the live state file."""
    for raw in [0.20] * 44 + [0.47] + [0.505] * 5:
        floor_calibration.maybe_record_ambient_sample(HARNESS, _ambient(raw))
    floor, basis = floor_calibration.resolve_gate("cc")
    assert (floor, basis) == (0.52, "raw"), (floor, basis)


def test_briefing_quotes_the_floor_the_gate_judged_not_the_one_it_moved_to(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    monkeypatch.setattr(state_mod, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(state_mod, "ENGINE_DIR", tmp_path)
    monkeypatch.setattr(floor_calibration, "STATE_FILE", tmp_path / "floor_calibration.json")
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [HARNESS])
    monkeypatch.setattr(command_actuator, "load_quiet_hours", lambda *a, **kw: None)
    monkeypatch.setattr(runner_mod, "escalate_for_action", lambda intent, dry_run=False: {"ok": True})

    captured = []

    def _capture(hc, briefing, dry_run=False):
        captured.append(briefing)
        return {"harness": hc["name"], "ok": True, "dry_run": True}

    monkeypatch.setattr(command_actuator, "invoke_harness", _capture)

    _seed_floor_at_0_520()

    runner = StreamRunner(dry_run=True, include_default_scanners=False)
    monkeypatch.setattr(runner.planner, "propose_plan", lambda content, budget: None)
    winner = {
        "source_module": "world_signals", "salience": 0.605,
        "content": "Seattle Times and Newsday are the latest publications to sue OpenAI",
        "context": {"raw_salience": 0.55, "top_url": "https://example.test/x"},
    }

    results = runner._dispatch_to_harnesses(None, winner, {})

    assert [r["ok"] for r in results] == [True]
    (briefing,) = captured
    # The decision was made against 0.520 — that is the bar the session
    # must be shown.
    assert "bar 0.520 fitted on the raw series" in briefing, briefing
    assert "bar 0.555" not in briefing, briefing
    assert "judged on min(bid, modulated) = 0.55" in briefing
    # ...and the calibration side-effect is untouched: this winner still
    # became a sample and the floor really did move to 0.555 for NEXT cycle.
    assert floor_calibration.resolve_gate("cc") == (0.555, "raw")
