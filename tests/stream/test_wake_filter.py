"""Per-harness wake filter: wake_modules allowlist + wake_min_salience floor.

Includes a through-the-loader test: the rehearsal bug was the config
normalizer dropping wake_modules before the filter ever saw it, which
direct-dict unit tests could not catch.
"""
import json

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
