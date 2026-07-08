"""Per-harness wake filter: wake_modules allowlist + wake_min_salience floor."""
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
