"""Tests for act/floor_calibration.py — the opt-in self-calibrating wake
floor (EXP-004t productized, issue #12). Zero coverage before this change."""
from __future__ import annotations

import json

import act.floor_calibration as floor_calibration
import continuity.journal as journal


def _patch(tmp_path, monkeypatch):
    monkeypatch.setattr(floor_calibration, "STATE_FILE", tmp_path / "floor_calibration.json")
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")


def _hc(name="h", **overrides):
    base = {"name": name, "wake_min_salience": "calibrate"}
    base.update(overrides)
    return base


def _winner(module="repo_issues", salience=0.3, alarm=False):
    d = {"source_module": module, "salience": salience}
    if alarm:
        d["context"] = {"alarm_lane": True}
    return d


# --- resolve_floor -------------------------------------------------------

def test_resolve_floor_defaults_to_warmup_before_any_samples(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    assert floor_calibration.resolve_floor("h") == floor_calibration.WARMUP_FLOOR


def test_resolve_floor_unknown_harness_defaults_to_warmup(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    for i in range(floor_calibration.CALIBRATION_WINDOW):
        floor_calibration.maybe_record_ambient_sample(_hc("h1"), _winner(salience=0.2))
    assert floor_calibration.resolve_floor("h2") == floor_calibration.WARMUP_FLOOR


# --- maybe_record_ambient_sample: eligibility gates --------------------------

def test_non_calibrate_config_is_ignored(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    hc = _hc(wake_min_salience=0.6)
    floor_calibration.maybe_record_ambient_sample(hc, _winner(salience=0.9))
    assert floor_calibration.resolve_floor("h") == floor_calibration.WARMUP_FLOOR
    assert not floor_calibration.STATE_FILE.exists()


def test_alarm_lane_winner_is_not_ambient(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    for _ in range(floor_calibration.CALIBRATION_WINDOW):
        floor_calibration.maybe_record_ambient_sample(_hc(), _winner(salience=0.95, alarm=True))
    # Never accumulated -> never calibrated -> still warmup.
    assert floor_calibration.resolve_floor("h") == floor_calibration.WARMUP_FLOOR


def test_wake_modules_filter_excludes_non_matching_winner(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    hc = _hc(wake_modules=["repo_issues"])
    for _ in range(floor_calibration.CALIBRATION_WINDOW):
        floor_calibration.maybe_record_ambient_sample(hc, _winner(module="priority_goals", salience=0.9))
    assert floor_calibration.resolve_floor("h") == floor_calibration.WARMUP_FLOOR


def test_none_winner_is_ignored(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    floor_calibration.maybe_record_ambient_sample(_hc(), None)
    assert not floor_calibration.STATE_FILE.exists()


def test_non_numeric_salience_is_ignored(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    floor_calibration.maybe_record_ambient_sample(_hc(), _winner(salience="high"))
    assert not floor_calibration.STATE_FILE.exists()


def test_missing_name_is_ignored(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    hc = {"wake_min_salience": "calibrate"}
    floor_calibration.maybe_record_ambient_sample(hc, _winner())
    assert not floor_calibration.STATE_FILE.exists()


# --- calibration completing --------------------------------------------------

def test_calibrates_to_max_ambient_plus_margin(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    hc = _hc()
    saliences = [0.1] * (floor_calibration.CALIBRATION_WINDOW - 1) + [0.42]
    for s in saliences:
        floor_calibration.maybe_record_ambient_sample(hc, _winner(salience=s))
    expected = round(0.42 + floor_calibration.CALIBRATION_MARGIN, 4)
    assert floor_calibration.resolve_floor("h") == expected


def test_stops_collecting_once_calibrated(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    hc = _hc()
    for _ in range(floor_calibration.CALIBRATION_WINDOW):
        floor_calibration.maybe_record_ambient_sample(hc, _winner(salience=0.3))
    floor_before = floor_calibration.resolve_floor("h")
    # A much higher ambient winner after calibration must NOT move the floor.
    floor_calibration.maybe_record_ambient_sample(hc, _winner(salience=0.99))
    assert floor_calibration.resolve_floor("h") == floor_before


def test_journals_exactly_once_on_completion(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    hc = _hc()
    for _ in range(floor_calibration.CALIBRATION_WINDOW + 5):
        floor_calibration.maybe_record_ambient_sample(hc, _winner(salience=0.3))
    lines = journal.JOURNAL_FILE.read_text().splitlines()
    calibrated_events = [json.loads(l) for l in lines if json.loads(l)["kind"] == "floor_calibrated"]
    assert len(calibrated_events) == 1
    assert calibrated_events[0]["harness"] == "h"


def test_harnesses_calibrate_independently(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    for _ in range(floor_calibration.CALIBRATION_WINDOW):
        floor_calibration.maybe_record_ambient_sample(_hc("a"), _winner(salience=0.2))
    assert floor_calibration.resolve_floor("a") != floor_calibration.WARMUP_FLOOR
    assert floor_calibration.resolve_floor("b") == floor_calibration.WARMUP_FLOOR


def test_never_raises_on_corrupt_state_file(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    floor_calibration.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    floor_calibration.STATE_FILE.write_text("not json", encoding="utf-8")
    assert floor_calibration.resolve_floor("h") == floor_calibration.WARMUP_FLOOR
    floor_calibration.maybe_record_ambient_sample(_hc(), _winner())  # must not raise
