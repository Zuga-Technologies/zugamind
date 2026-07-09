"""Tests for the operational-truth freshness gate (gates/operational_truth.py)."""
from gates import operational_truth as ot


def test_snapshot_returns_ts_and_ports():
    snap = ot.snapshot(force=True)
    assert "ts" in snap and "ports" in snap


def test_format_block_empty_when_no_ports():
    assert ot.format_block({"ts": "00:00:00", "ports": {}}) == ""


def test_format_block_lists_up_and_down():
    block = ot.format_block({"ts": "12:00:00", "ports": {8000: True, 8001: False}})
    assert "services UP" in block and "services DOWN" in block


def test_is_stale_operational_flags_percycle_memory_claim():
    assert ot.is_stale_operational("memory usage is 40MB per cycle, growing") is True


def test_is_stale_operational_false_on_clean_text():
    assert ot.is_stale_operational("everything looks fine this cycle") is False


def test_is_stale_operational_never_raises_on_empty():
    assert ot.is_stale_operational("") is False
    assert ot.is_stale_operational(None) is False  # type: ignore[arg-type]
