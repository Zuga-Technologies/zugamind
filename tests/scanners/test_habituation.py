"""Tests for scanners.habituation_filter — repeat-trigger damping.

The claim under test is the README's cron-comparison line: a seen trigger
is damped for HABITUATION_HOURS, then re-emits. Also covers the
`bypass_habituation` 60-minute cooldown from scanners/_template.py, the
detail-hash key fallback, and fail-silent behavior on a corrupt seen-file.
"""
import json

import foundation.config as config
from scanners import habituation_filter


def _patch_seen_file(tmp_path, monkeypatch):
    seen_file = tmp_path / "seen_triggers.json"
    monkeypatch.setattr(config, "SEEN_TRIGGERS_FILE", seen_file)
    return seen_file


def _story(story_id=101):
    return {"type": "hn_story", "story_id": story_id, "detail": "Some AI story",
            "novelty": 0.8, "relevance": 0.7, "urgency": 0.3}


def test_first_sighting_passes_and_is_recorded(tmp_path, monkeypatch):
    seen_file = _patch_seen_file(tmp_path, monkeypatch)

    out = habituation_filter([_story()], now=1_000_000.0)

    assert len(out) == 1
    seen = json.loads(seen_file.read_text())
    assert seen == {"hn_story:101": 1_000_000.0}


def test_repeat_within_window_is_damped(tmp_path, monkeypatch):
    _patch_seen_file(tmp_path, monkeypatch)

    assert len(habituation_filter([_story()], now=1_000_000.0)) == 1
    # One hour later — well inside the default 6h window.
    assert habituation_filter([_story()], now=1_000_000.0 + 3600) == []


def test_reemits_after_window_expires(tmp_path, monkeypatch):
    _patch_seen_file(tmp_path, monkeypatch)
    window = config.HABITUATION_HOURS * 3600

    assert len(habituation_filter([_story()], now=1_000_000.0)) == 1
    assert len(habituation_filter([_story()], now=1_000_000.0 + window + 1)) == 1


def test_bypass_habituation_uses_60min_cooldown(tmp_path, monkeypatch):
    _patch_seen_file(tmp_path, monkeypatch)
    t = {"type": "heartbeat_source", "url": "https://example.invalid/x",
         "detail": "repeat is the signal", "bypass_habituation": True}

    assert len(habituation_filter([t], now=1_000_000.0)) == 1
    assert habituation_filter([t], now=1_000_000.0 + 1800) == []      # 30min: damped
    assert len(habituation_filter([t], now=1_000_000.0 + 3601)) == 1  # >60min: re-emits


def test_key_falls_back_to_detail_hash_without_explicit_id(tmp_path, monkeypatch):
    _patch_seen_file(tmp_path, monkeypatch)
    a = {"type": "note", "detail": "identical text"}
    b = {"type": "note", "detail": "identical text"}
    c = {"type": "note", "detail": "different text"}

    out = habituation_filter([a, b, c], now=1_000_000.0)
    # a and b share a key — b is damped within the same batch; c is distinct.
    assert out == [a, c]


def test_corrupt_seen_file_fails_open_to_fresh(tmp_path, monkeypatch):
    seen_file = _patch_seen_file(tmp_path, monkeypatch)
    seen_file.write_text("{not json at all")

    out = habituation_filter([_story()], now=1_000_000.0)

    assert len(out) == 1  # a broken state file must never eat a fresh trigger
    assert json.loads(seen_file.read_text()) == {"hn_story:101": 1_000_000.0}


def test_stale_entries_are_pruned(tmp_path, monkeypatch):
    seen_file = _patch_seen_file(tmp_path, monkeypatch)
    window = config.HABITUATION_HOURS * 3600
    seen_file.write_text(json.dumps({"hn_story:old": 1.0}))  # ancient

    habituation_filter([_story()], now=1_000_000.0 + window * 2)

    seen = json.loads(seen_file.read_text())
    assert "hn_story:old" not in seen  # pruned — the file stays bounded
    assert "hn_story:101" in seen
