"""Tests for the consciousness-integrity drift gate (gates/integrity.py)."""
import math

from gates import integrity


def test_insufficient_samples_returns_stable_with_sample_count():
    report = integrity.compute_consciousness_integrity([0.1, 0.2, 0.3])
    assert report["severity"] == "STABLE"
    assert report["samples"] == 3


def test_degenerate_series_tagged_insufficient():
    series = [0.05] * 15  # zero variance
    report = integrity.compute_consciousness_integrity(series)
    assert report["severity"] == "STABLE"
    assert report["analysis"] == "insufficient"


def test_stationary_series_reports_stable():
    # Mean-reverting oscillation around 0 — should read as stationary.
    series = [0.1 * math.sin(i) for i in range(40)]
    report = integrity.compute_consciousness_integrity(series)
    assert report["severity"] == "STABLE"
    assert report["is_stationary"] is True


def test_trending_series_reports_critical_or_drifting():
    # Upward trend with noise -- a perfectly linear ramp is a zero-residual
    # "perfect fit" that _dickey_fuller correctly treats as undetermined.
    series = [i * 0.05 + 0.03 * math.sin(i * 3) for i in range(30)]
    report = integrity.compute_consciousness_integrity(series)
    assert report["severity"] in ("CRITICAL", "DRIFTING")
    assert report["trend_slope"] > 0


def test_report_shape_has_expected_keys():
    series = [0.1 * math.sin(i) for i in range(40)]
    report = integrity.compute_consciousness_integrity(series)
    for key in (
        "severity", "adf_statistic", "adf_p_value", "is_stationary",
        "trend_slope", "trend_direction", "samples", "recent_mean_drift",
        "recommendation",
    ):
        assert key in report


def test_never_raises_on_garbage_input():
    assert integrity.compute_consciousness_integrity([])["severity"] == "STABLE"
    assert integrity.compute_consciousness_integrity([0.1])["severity"] == "STABLE"
    # constant series past the sample floor — degenerate, not an exception
    report = integrity.compute_consciousness_integrity([1.0] * 12)
    assert report["severity"] == "STABLE"
