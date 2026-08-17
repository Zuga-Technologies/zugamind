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

def test_calibrates_to_quantile_plus_margin_ignoring_outliers(tmp_path, monkeypatch):
    """The 1.04 regression, prevented at the root: two 0.99 outliers among
    mostly-quiet ambient samples must NOT own the floor (max would have set
    0.99+0.05=1.04; the p90 quantile stays with the quiet majority)."""
    _patch(tmp_path, monkeypatch)
    hc = _hc()
    saliences = [0.25] * (floor_calibration.CALIBRATION_WINDOW - 2) + [0.99, 0.99]
    for s in saliences:
        floor_calibration.maybe_record_ambient_sample(hc, _winner(salience=s))
    # p90 of (18x0.25, 2x0.99) = 0.25 -> +margin = 0.30 -> clamped up to warmup
    assert floor_calibration.resolve_floor("h") == floor_calibration.WARMUP_FLOOR


def test_floor_drifts_with_rolling_window(tmp_path, monkeypatch):
    """When the environment changes (e.g. new feeds raise normal winner
    salience), the floor must follow instead of staying frozen forever."""
    _patch(tmp_path, monkeypatch)
    hc = _hc()
    for _ in range(floor_calibration.CALIBRATION_WINDOW):
        floor_calibration.maybe_record_ambient_sample(hc, _winner(salience=0.25))
    quiet_floor = floor_calibration.resolve_floor("h")
    for _ in range(floor_calibration.ROLLING_WINDOW):
        floor_calibration.maybe_record_ambient_sample(hc, _winner(salience=0.6))
    loud_floor = floor_calibration.resolve_floor("h")
    assert loud_floor > quiet_floor
    assert loud_floor == round(0.6 + floor_calibration.CALIBRATION_MARGIN, 4)


def test_rolling_window_caps_sample_count(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    hc = _hc()
    for _ in range(floor_calibration.ROLLING_WINDOW + 25):
        floor_calibration.maybe_record_ambient_sample(hc, _winner(salience=0.3))
    state = json.loads(floor_calibration.STATE_FILE.read_text())
    assert len(state["h"]["samples"]) == floor_calibration.ROLLING_WINDOW


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
        floor_calibration.maybe_record_ambient_sample(_hc("a"), _winner(salience=0.6))
    assert floor_calibration.resolve_floor("a") == round(
        0.6 + floor_calibration.CALIBRATION_MARGIN, 4)
    assert floor_calibration.resolve_floor("b") == floor_calibration.WARMUP_FLOOR


def test_never_raises_on_corrupt_state_file(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    floor_calibration.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    floor_calibration.STATE_FILE.write_text("not json", encoding="utf-8")
    assert floor_calibration.resolve_floor("h") == floor_calibration.WARMUP_FLOOR
    floor_calibration.maybe_record_ambient_sample(_hc(), _winner())  # must not raise


def test_floor_never_exceeds_ceiling(tmp_path, monkeypatch):
    """Regression: 0.99 'ambient' samples calibrated a 1.04 floor — above the
    1.0 salience bound, silently disabling every non-alarm wake (live bug,
    2026-08-06; wakes dead since calibration day Aug 3)."""
    _patch(tmp_path, monkeypatch)
    hc = _hc()
    for _ in range(floor_calibration.CALIBRATION_WINDOW):
        floor_calibration.maybe_record_ambient_sample(hc, _winner(salience=0.99))
    assert floor_calibration.resolve_floor("h") <= floor_calibration.FLOOR_CEILING


def test_resolve_clamps_preexisting_bad_floor(tmp_path, monkeypatch):
    """A state file calibrated before the ceiling existed must heal on read."""
    _patch(tmp_path, monkeypatch)
    floor_calibration.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    floor_calibration.STATE_FILE.write_text(
        json.dumps({"h": {"samples": [], "floor": 1.04, "calibrated_at": "x"}}),
        encoding="utf-8")
    assert floor_calibration.resolve_floor("h") == floor_calibration.FLOOR_CEILING


# --- raw vs modulated basis (2026-08-17) ---------------------------------

def _raw_winner(module="world_signals", salience=0.6816, raw=0.5164):
    """The 2026-08-17 wake, to the digit: a 0.5164 bid the attention schema
    multiplied by 1.2 (streak-break) then 1.1 (not-current-focus)."""
    return {"source_module": module, "salience": salience, "content": "HN story",
            "context": {"raw_salience": raw}}


def test_raw_series_is_skipped_when_unstamped(tmp_path, monkeypatch):
    """A winner with no raw_salience must NOT seed the raw window with its
    boosted number — that contamination is what the parallel series avoids."""
    _patch(tmp_path, monkeypatch)
    for _ in range(floor_calibration.CALIBRATION_WINDOW):
        floor_calibration.maybe_record_ambient_sample(_hc(), _winner(salience=0.3))
    floor, basis = floor_calibration.resolve_gate("h")
    assert basis == "modulated"
    state = json.loads(floor_calibration.STATE_FILE.read_text(encoding="utf-8"))
    assert not state["h"].get("raw_samples")


def test_basis_stays_modulated_until_raw_window_fills(tmp_path, monkeypatch):
    """The whole safety property: while the raw series is still filling, the
    gate behaves byte-for-byte as it does today."""
    _patch(tmp_path, monkeypatch)
    for _ in range(floor_calibration.CALIBRATION_WINDOW):
        floor_calibration.maybe_record_ambient_sample(_hc(), _winner(salience=0.3))
    for _ in range(floor_calibration.CALIBRATION_WINDOW - 1):
        floor_calibration.maybe_record_ambient_sample(_hc(), _raw_winner())
    floor, basis = floor_calibration.resolve_gate("h")
    assert basis == "modulated"
    assert floor == floor_calibration.resolve_floor("h")


def test_basis_switches_to_raw_once_raw_window_fills(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    for _ in range(floor_calibration.CALIBRATION_WINDOW):
        floor_calibration.maybe_record_ambient_sample(_hc(), _raw_winner())
    floor, basis = floor_calibration.resolve_gate("h")
    assert basis == "raw"
    # Fitted on the RAW samples (0.5164), not the modulated ones (0.6816).
    assert floor == floor_calibration._quantile_floor(
        [0.5164] * floor_calibration.CALIBRATION_WINDOW)


def test_basis_switch_is_journaled_once(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    for _ in range(floor_calibration.CALIBRATION_WINDOW + 5):
        floor_calibration.maybe_record_ambient_sample(_hc(), _raw_winner())
    events = [json.loads(l) for l in
              floor_calibration.STATE_FILE.parent.joinpath("journal.jsonl")
              .read_text(encoding="utf-8").splitlines() if l.strip()]
    switches = [e for e in events if e.get("kind") == "floor_basis_switched"]
    assert len(switches) == 1
    assert switches[0]["basis"] == "raw"


def test_a_floor_fitted_on_one_basis_never_judges_the_other(tmp_path, monkeypatch):
    """The trap this design exists to avoid: comparing raw numbers against a
    floor fitted on boosted ones silently raises the bar."""
    _patch(tmp_path, monkeypatch)
    floor_calibration.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    floor_calibration.STATE_FILE.write_text(json.dumps({"h": {
        "samples": [0.6] * 50, "floor": 0.655, "calibrated_at": "x",
        "raw_samples": [0.5] * 3, "raw_floor": None,
    }}), encoding="utf-8")
    floor, basis = floor_calibration.resolve_gate("h")
    assert (floor, basis) == (0.655, "modulated")
