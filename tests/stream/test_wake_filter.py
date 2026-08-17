"""Per-harness wake filter: wake_modules allowlist + wake_min_salience floor.

Includes a through-the-loader test: the rehearsal bug was the config
normalizer dropping wake_modules before the filter ever saw it, which
direct-dict unit tests could not catch.
"""
import json

import act.floor_calibration as floor_calibration
from act.command_actuator import load_harness_configs
from stream.runner import StreamRunner


def _winner(module="repo_issues", salience=0.8):
    return {"source_module": module, "salience": salience, "content": "x"}


def test_no_filter_wakes_for_anything():
    assert StreamRunner._harness_wants({}, _winner("priority_goals", 0.1))


def test_wake_modules_allowlist():
    hc = {"wake_modules": ["repo_issues"]}
    assert StreamRunner._harness_wants(hc, _winner("repo_issues"))
    assert not StreamRunner._harness_wants(hc, _winner("priority_goals"))


def test_wake_min_salience_floor():
    hc = {"wake_min_salience": 0.6}
    assert StreamRunner._harness_wants(hc, _winner(salience=0.7))
    assert not StreamRunner._harness_wants(hc, _winner(salience=0.5))


def test_filters_compose():
    hc = {"wake_modules": ["repo_issues"], "wake_min_salience": 0.6}
    assert StreamRunner._harness_wants(hc, _winner("repo_issues", 0.7))
    assert not StreamRunner._harness_wants(hc, _winner("repo_issues", 0.5))
    assert not StreamRunner._harness_wants(hc, _winner("metacognition", 0.9))


def test_malformed_salience_fails_closed():
    hc = {"wake_min_salience": 0.6}
    assert not StreamRunner._harness_wants(hc, {"source_module": "m", "salience": "high"})


def test_wake_filters_survive_the_config_loader(tmp_path):
    p = tmp_path / "harness.json"
    p.write_text(json.dumps({"harnesses": [{
        "name": "h", "command": ["echo", "{briefing_file}"],
        "wake_modules": ["repo_issues"], "wake_min_salience": 0.6,
    }]}), encoding="utf-8")
    (cfg,) = load_harness_configs(p)
    assert cfg["wake_modules"] == ["repo_issues"]
    assert cfg["wake_min_salience"] == 0.6
    assert not StreamRunner._harness_wants(cfg, _winner("priority_goals", 0.9))
    assert StreamRunner._harness_wants(cfg, _winner("repo_issues", 0.9))


# --- "calibrate" mode (issue #12) --------------------------------------------

def test_calibrate_string_survives_the_config_loader(tmp_path):
    p = tmp_path / "harness.json"
    p.write_text(json.dumps({"harnesses": [{
        "name": "h", "command": ["echo", "{briefing_file}"],
        "wake_min_salience": "calibrate",
    }]}), encoding="utf-8")
    (cfg,) = load_harness_configs(p)
    assert cfg["wake_min_salience"] == "calibrate"


def test_calibrate_mode_uses_warmup_floor_before_calibration(tmp_path, monkeypatch):
    monkeypatch.setattr(floor_calibration, "STATE_FILE", tmp_path / "floor_calibration.json")
    hc = {"name": "h", "wake_min_salience": "calibrate"}
    assert not StreamRunner._harness_wants(hc, _winner(salience=0.2))
    assert StreamRunner._harness_wants(hc, _winner(salience=0.4))


def test_calibrate_mode_uses_learned_floor_once_calibrated(tmp_path, monkeypatch):
    monkeypatch.setattr(floor_calibration, "STATE_FILE", tmp_path / "floor_calibration.json")
    import continuity.journal as journal
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")

    hc = {"name": "h", "wake_min_salience": "calibrate"}
    # 0.6 ambient keeps the learned floor above the WARMUP_FLOOR lower clamp,
    # so this asserts the LEARNED value is enforced, not the warmup default.
    for _ in range(floor_calibration.CALIBRATION_WINDOW):
        floor_calibration.maybe_record_ambient_sample(hc, _winner(salience=0.6))
    learned = round(0.6 + floor_calibration.CALIBRATION_MARGIN, 4)
    assert learned > floor_calibration.WARMUP_FLOOR
    assert not StreamRunner._harness_wants(hc, _winner(salience=learned - 0.01))
    assert StreamRunner._harness_wants(hc, _winner(salience=learned))


def test_alarm_lane_bypasses_calibrate_floor_too(tmp_path, monkeypatch):
    monkeypatch.setattr(floor_calibration, "STATE_FILE", tmp_path / "floor_calibration.json")
    hc = {"name": "h", "wake_min_salience": "calibrate"}
    winner = _winner(salience=0.01)
    winner["context"] = {"alarm_lane": True}
    assert StreamRunner._harness_wants(hc, winner)


# --- raw-basis gating (2026-08-17) ---------------------------------------

def _boosted(raw=0.5164, salience=0.6816):
    """The real 2026-08-17 winner: bid 0.5164, journaled 0.6816 after a x1.2
    streak-break boost and a x1.1 not-current-focus boost."""
    return {"source_module": "world_signals", "salience": salience,
            "content": "HN [202pts]", "context": {"raw_salience": raw}}


def test_static_floor_still_judges_the_modulated_number(monkeypatch):
    """A hand-set wake_min_salience was written against `salience`; changing
    its meaning underneath every fleet config would be a silent re-tune."""
    hc = {"wake_min_salience": 0.6}
    assert StreamRunner._harness_wants(hc, _boosted())


def test_raw_basis_refuses_a_wake_the_boost_would_have_bought(tmp_path, monkeypatch):
    monkeypatch.setattr(floor_calibration, "resolve_gate", lambda n: (0.655, "raw"))
    hc = {"name": "h", "wake_min_salience": "calibrate"}
    # 0.6816 clears 0.655; 0.5164 — what it actually asked for — does not.
    assert not StreamRunner._harness_wants(hc, _boosted())


def test_raw_basis_still_wakes_when_the_signal_earns_it(monkeypatch):
    monkeypatch.setattr(floor_calibration, "resolve_gate", lambda n: (0.655, "raw"))
    hc = {"name": "h", "wake_min_salience": "calibrate"}
    assert StreamRunner._harness_wants(hc, _boosted(raw=0.9, salience=0.99))


def test_modulated_basis_is_unchanged_behaviour(monkeypatch):
    monkeypatch.setattr(floor_calibration, "resolve_gate", lambda n: (0.655, "modulated"))
    hc = {"name": "h", "wake_min_salience": "calibrate"}
    assert StreamRunner._harness_wants(hc, _boosted())


def test_unstamped_bid_is_not_a_free_pass_under_raw_basis(monkeypatch):
    """No raw_salience -> fall back to `salience`, never wave it through."""
    monkeypatch.setattr(floor_calibration, "resolve_gate", lambda n: (0.655, "raw"))
    hc = {"name": "h", "wake_min_salience": "calibrate"}
    assert not StreamRunner._harness_wants(
        hc, {"source_module": "m", "salience": 0.1, "content": "x"})
    assert StreamRunner._harness_wants(
        hc, {"source_module": "m", "salience": 0.9, "content": "x"})


def test_alarm_lane_still_bypasses_under_raw_basis(monkeypatch):
    """A critical must surface even when its raw bid is below the floor."""
    monkeypatch.setattr(floor_calibration, "resolve_gate", lambda n: (0.655, "raw"))
    hc = {"name": "h", "wake_min_salience": "calibrate"}
    w = _boosted(raw=0.01, salience=0.02)
    w["context"]["alarm_lane"] = True
    assert StreamRunner._harness_wants(hc, w)
