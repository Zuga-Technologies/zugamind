"""Tests for the session-signals hook pack:
examples/hooks/zugamind_signals.py (producer) and
examples/custom-scanners/scan_session_signals.py (consumer).

Both dirs are non-importable-package dirs (dash in the name / no __init__),
so they go on sys.path directly, same as test_agent_reach.py does.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_HOOKS_DIR = Path(__file__).resolve().parent.parent.parent / "examples" / "hooks"
_SCANNER_DIR = Path(__file__).resolve().parent.parent.parent / "examples" / "custom-scanners"
for _d in (_HOOKS_DIR, _SCANNER_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import scan_session_signals  # noqa: E402
import zugamind_signals  # noqa: E402


def _wire_tmp(monkeypatch, tmp_path):
    feed = tmp_path / "engine" / "session_signals.jsonl"
    cursors = tmp_path / "engine" / "hook_cursors"
    monkeypatch.setattr(zugamind_signals, "_FEED", feed)
    monkeypatch.setattr(zugamind_signals, "_CURSOR_DIR", cursors)
    monkeypatch.setattr(scan_session_signals, "_FEED", feed)
    monkeypatch.setattr(scan_session_signals, "_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(scan_session_signals, "_CACHE_DIR", tmp_path)
    return feed, cursors


def _feed_lines(feed: Path) -> list[dict]:
    return [json.loads(l) for l in feed.read_text(encoding="utf-8").splitlines() if l.strip()]


# ------------------------------------------------------------ hook: stop --

def test_stop_writes_pulse_and_collapses_gist(monkeypatch, tmp_path):
    feed, _ = _wire_tmp(monkeypatch, tmp_path)
    zugamind_signals._mode_stop({
        "session_id": "s1", "cwd": "E:/repo",
        "last_assistant_message": "line one\nline two\t" + "x" * 500,
    })
    (rec,) = _feed_lines(feed)
    assert rec["kind"] == "human_session_pulse"
    assert "\n" not in rec["gist"] and len(rec["gist"]) <= 200
    assert rec["gist"].startswith("line one line two")


def test_stop_skips_when_stop_hook_active(monkeypatch, tmp_path):
    feed, _ = _wire_tmp(monkeypatch, tmp_path)
    zugamind_signals._mode_stop({"session_id": "s1", "stop_hook_active": True})
    assert not feed.exists()


def test_stop_sweeps_only_stale_cursors(monkeypatch, tmp_path):
    _, cursors = _wire_tmp(monkeypatch, tmp_path)
    cursors.mkdir(parents=True)
    stale = cursors / "old.json"
    fresh = cursors / "new.json"
    stale.write_text("{}")
    fresh.write_text("{}")
    old = time.time() - 15 * 24 * 3600
    import os
    os.utime(stale, (old, old))
    zugamind_signals._mode_stop({"session_id": "s1", "last_assistant_message": "hi"})
    assert not stale.exists() and fresh.exists()


# ----------------------------------------------------- hook: session-end --

def test_session_end_records_and_deletes_own_cursor(monkeypatch, tmp_path):
    feed, cursors = _wire_tmp(monkeypatch, tmp_path)
    cursors.mkdir(parents=True)
    (cursors / "abc-123.json").write_text("{}")
    zugamind_signals._mode_session_end({"session_id": "abc-123", "reason": "exit"})
    (rec,) = _feed_lines(feed)
    assert rec["kind"] == "human_session_end" and rec["reason"] == "exit"
    assert not (cursors / "abc-123.json").exists()


# ---------------------------------------------------- hook: notification --

def test_notification_filters_types(monkeypatch, tmp_path):
    feed, _ = _wire_tmp(monkeypatch, tmp_path)
    zugamind_signals._mode_notification({"notification_type": "auth_success", "message": "no"})
    zugamind_signals._mode_notification({"notification_type": "permission_prompt", "message": "yes"})
    recs = _feed_lines(feed)
    assert len(recs) == 1 and recs[0]["type"] == "permission_prompt"


def test_notification_tolerates_alternate_field_names(monkeypatch, tmp_path):
    feed, _ = _wire_tmp(monkeypatch, tmp_path)
    zugamind_signals._mode_notification({"type": "idle_prompt", "notification_text": "waiting"})
    (rec,) = _feed_lines(feed)
    assert rec["type"] == "idle_prompt" and rec["message"] == "waiting"


# ----------------------------------------------------------- feed bounds --

def test_feed_bounding_keeps_newest_tail(monkeypatch, tmp_path):
    feed, _ = _wire_tmp(monkeypatch, tmp_path)
    monkeypatch.setattr(zugamind_signals, "_FEED_MAX_BYTES", 500)
    monkeypatch.setattr(zugamind_signals, "_FEED_KEEP_LINES", 3)
    for i in range(20):
        zugamind_signals._append_signal({"kind": "human_session_pulse", "i": i})
    recs = _feed_lines(feed)
    # bounded at least once (20 lines went in), newest survives, and the file
    # never drifts far past the cap (one append's worth of slack at most)
    assert len(recs) < 20
    assert recs[-1]["i"] == 19
    assert feed.stat().st_size < 500 + 100
    assert not feed.with_suffix(".tmp").exists()


# --------------------------------------------------------------- scanner --

def test_scanner_consumes_each_line_once_and_caps(monkeypatch, tmp_path):
    feed, _ = _wire_tmp(monkeypatch, tmp_path)
    for i in range(8):
        zugamind_signals._mode_notification(
            {"notification_type": "permission_prompt", "message": f"m{i}"})
    out = scan_session_signals.scan_session_signals()
    assert len(out) == 5  # capped
    for t in out:
        assert t["type"] == "claude_needs_human"
        assert len(t["detail"]) <= 280
        for k in ("novelty", "relevance", "urgency"):
            assert 0.0 <= t[k] <= 1.0
    # cursor advanced past EVERYTHING (capped items are dropped, not replayed)
    assert scan_session_signals.scan_session_signals() == []


def test_scanner_salience_shape(monkeypatch, tmp_path):
    feed, _ = _wire_tmp(monkeypatch, tmp_path)
    zugamind_signals._mode_notification(
        {"notification_type": "agent_needs_input", "message": "blocked"})
    zugamind_signals._mode_stop({"session_id": "s", "cwd": "E:/r", "last_assistant_message": "hi"})
    urgent, ambient = scan_session_signals.scan_session_signals()
    assert urgent["urgency"] > ambient["urgency"]
    assert ambient["type"] == "human_activity"


def test_scanner_handles_feed_rewrite(monkeypatch, tmp_path):
    feed, _ = _wire_tmp(monkeypatch, tmp_path)
    zugamind_signals._mode_stop({"session_id": "s", "last_assistant_message": "one"})
    assert len(scan_session_signals.scan_session_signals()) == 1
    # simulate the hook's bounding rewrite shrinking the file below the cursor
    feed.write_text(
        json.dumps({"ts": "t", "kind": "human_session_end", "reason": "exit", "cwd": ""}) + "\n",
        encoding="utf-8")
    out = scan_session_signals.scan_session_signals()
    assert len(out) == 1 and "session ended" in out[0]["detail"]


def test_scanner_fail_silent(monkeypatch, tmp_path):
    _wire_tmp(monkeypatch, tmp_path)
    assert scan_session_signals.scan_session_signals() == []  # no feed at all


def test_hook_main_never_raises_and_exits_zero(monkeypatch, tmp_path, capsys):
    _wire_tmp(monkeypatch, tmp_path)
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json at all"))
    assert zugamind_signals.main(["stop"]) == 0
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert zugamind_signals.main(["unknown-mode"]) == 0
    assert capsys.readouterr().out == ""  # hooks stay silent on stdout
