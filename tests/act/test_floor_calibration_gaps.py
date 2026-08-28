"""act/floor_calibration.py — the 2026-08-28 audit gaps.

Torn state file, poisoned samples wedging a harness forever, the stale
basis-blind resolve_floor, silent floor drift, and naive-local timestamps.
"""
from __future__ import annotations

import json

import act.floor_calibration as floor_calibration
import continuity.journal as journal
from foundation import fs


def _patch(tmp_path, monkeypatch):
    monkeypatch.setattr(floor_calibration, "STATE_FILE", tmp_path / "floor_calibration.json")
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")


def _hc(name="h"):
    return {"name": name, "wake_min_salience": "calibrate"}


def _winner(salience=0.3, raw=None):
    d = {"source_module": "repo_issues", "salience": salience}
    if raw is not None:
        d["context"] = {"raw_salience": raw}
    return d


def _events(kind):
    return [e for e in journal.read_events(limit=None) if e.get("kind") == kind]


def _fill(n=floor_calibration.CALIBRATION_WINDOW, salience=0.3, raw=None):
    for _ in range(n):
        floor_calibration.maybe_record_ambient_sample(_hc(), _winner(salience, raw))


# --- torn write ------------------------------------------------------------

def test_crash_mid_save_keeps_the_previous_state(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    _fill()
    before = json.loads(floor_calibration.STATE_FILE.read_text(encoding="utf-8"))
    assert before["h"]["floor"] is not None

    def boom(src, dst):
        raise OSError("simulated crash between write and replace")
    monkeypatch.setattr(fs.os, "replace", boom)
    floor_calibration.maybe_record_ambient_sample(_hc(), _winner(0.9))  # swallowed, logged

    after = json.loads(floor_calibration.STATE_FILE.read_text(encoding="utf-8"))
    assert after == before
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("floor_calibration")] == ["floor_calibration.json"]


# --- poisoned state --------------------------------------------------------

def test_poisoned_samples_are_dropped_and_the_file_heals(tmp_path, monkeypatch):
    """A str in `samples` used to raise inside _quantile_floor BEFORE
    _save_state, so the poison was never rewritten: wedged forever."""
    _patch(tmp_path, monkeypatch)
    floor_calibration.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    samples = [0.3] * 19 + ["not a number", True, None]
    floor_calibration.STATE_FILE.write_text(json.dumps({
        "h": {"samples": samples, "floor": "bad", "calibrated_at": None},
        "junk": "not an entry",
    }), encoding="utf-8")

    floor_calibration.maybe_record_ambient_sample(_hc(), _winner(0.3))

    state = json.loads(floor_calibration.STATE_FILE.read_text(encoding="utf-8"))
    assert "junk" not in state
    assert all(isinstance(v, float) for v in state["h"]["samples"])
    assert len(state["h"]["samples"]) == 20
    assert state["h"]["floor"] == floor_calibration._quantile_floor([0.3] * 20)
    assert floor_calibration.resolve_gate("h")[0] == state["h"]["floor"]


def test_non_dict_state_file_reads_as_empty(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    floor_calibration.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    floor_calibration.STATE_FILE.write_text("[1, 2, 3]", encoding="utf-8")
    assert floor_calibration._load_state() == {}
    floor_calibration.maybe_record_ambient_sample(_hc(), _winner())  # must not raise


# --- resolve_floor follows the basis ---------------------------------------

def test_resolve_floor_follows_the_raw_basis_once_switched(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    _fill(salience=0.8, raw=0.4)  # modulated floor ~0.85, raw floor ~0.45
    floor, basis = floor_calibration.resolve_gate("h")
    assert basis == "raw"
    assert floor_calibration.resolve_floor("h") == floor
    assert floor_calibration.resolve_floor("h") < floor_calibration._quantile_floor([0.8] * 20)


def test_resolve_floor_still_warms_up(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    assert floor_calibration.resolve_floor("nobody") == floor_calibration.WARMUP_FLOOR


# --- drift journaling ------------------------------------------------------

def test_floor_drift_is_journaled_once_it_moves_enough(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    _fill(salience=0.3)
    assert len(_events("floor_calibrated")) == 1
    assert _events("floor_drifted") == []

    # a tiny wobble below the delta stays silent
    floor_calibration.maybe_record_ambient_sample(_hc(), _winner(0.31))
    assert _events("floor_drifted") == []

    # the environment shifts: the rolling window fills with much louder noise
    _fill(n=floor_calibration.ROLLING_WINDOW, salience=0.7)
    drifts = _events("floor_drifted")
    assert drifts, "a 0.35 -> ~0.75 floor move must be visible in the journal"
    assert drifts[0]["basis"] == "modulated"
    assert drifts[-1]["to"] > drifts[0]["from"]
    assert all(d["at_ceiling"] is False for d in drifts)


def test_arriving_at_the_ceiling_is_flagged(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    _fill(salience=0.3)
    _fill(n=floor_calibration.ROLLING_WINDOW, salience=0.99)
    drifts = _events("floor_drifted")
    assert drifts and drifts[-1]["at_ceiling"] is True
    assert drifts[-1]["to"] == floor_calibration.FLOOR_CEILING


def test_first_calibration_at_the_ceiling_is_flagged_too(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    _fill(salience=0.99)
    assert _events("floor_calibrated")[0]["at_ceiling"] is True


# --- timestamps ------------------------------------------------------------

def test_calibrated_at_uses_the_journal_utc_convention(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    _fill(salience=0.3, raw=0.3)
    state = json.loads(floor_calibration.STATE_FILE.read_text(encoding="utf-8"))
    for key in ("calibrated_at", "raw_calibrated_at"):
        assert state["h"][key].endswith("+00:00"), key
