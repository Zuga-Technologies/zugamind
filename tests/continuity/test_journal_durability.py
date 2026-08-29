"""continuity/journal.py storage contract — the 2026-08-28 audit gaps.

The live journal (7,576 lines) carried four orphaned event tails, silently
skipped on every read; a payload could overwrite the journal's own `ts`;
`limit=0` returned everything; nothing bounded growth.
"""
from __future__ import annotations

import json
import logging

import pytest

import continuity.journal as journal


@pytest.fixture()
def jf(tmp_path, monkeypatch):
    path = tmp_path / "engine" / "journal.jsonl"
    monkeypatch.setattr(journal, "JOURNAL_FILE", path)
    monkeypatch.setattr(journal, "_appends_since_check", 0)
    monkeypatch.setattr(journal, "_tail_checked_for", None)   # each test is a "fresh process"
    return path


def _lines(path):
    return [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# --- the journal owns ts and kind ----------------------------------------------------

def test_payload_cannot_overwrite_ts_or_kind(jf, monkeypatch):
    monkeypatch.setattr(journal, "now_iso", lambda: "2026-08-28T12:00:00+00:00")
    journal.append_event("cycle", {"ts": "1999-01-01T00:00:00+00:00", "kind": "forged", "x": 1})
    (e,) = journal.read_events()
    assert e["ts"] == "2026-08-28T12:00:00+00:00" and e["kind"] == "cycle" and e["x"] == 1


def test_non_dict_payload_is_wrapped_not_dropped(jf):
    journal.append_event("note", ["not", "a", "dict"])  # type: ignore[arg-type]
    (e,) = journal.read_events()
    assert e["kind"] == "note" and e["payload"] == ["not", "a", "dict"]


# --- torn tails ------------------------------------------------------------------------

def test_torn_tail_does_not_swallow_the_next_event(jf, caplog):
    jf.parent.mkdir(parents=True)
    jf.write_bytes(b'{"ts": "2026-08-28T11:00:00+00:00", "kind": "cycle"}\n{"ts": "2026-08-28T11:07:00+00:00", "ki')
    journal.append_event("alarm", {"detail": "after the tear"})
    with caplog.at_level(logging.WARNING):
        events = journal.read_events()
    assert [e["kind"] for e in events] == ["cycle", "alarm"]      # the torn line is lost, the next one is NOT
    assert any("skipped 1 malformed" in r.message for r in caplog.records)


def test_each_event_is_one_line_even_with_embedded_newlines(jf):
    journal.append_event("harness_invocation", {"stdout": "line one\nline two\r\nthree"})
    assert len(_lines(jf)) == 1
    (e,) = journal.read_events()
    assert e["stdout"] == "line one\nline two\r\nthree"


def test_parent_directory_is_created_for_a_redirected_journal(tmp_path, monkeypatch):
    path = tmp_path / "deep" / "er" / "journal.jsonl"
    monkeypatch.setattr(journal, "JOURNAL_FILE", path)
    monkeypatch.setattr(journal, "_tail_checked_for", None)
    journal.append_event("cycle", {})
    assert path.exists() and len(journal.read_events()) == 1


def test_tail_is_checked_once_per_process_not_per_append(jf, monkeypatch):
    calls = []
    real = journal._ends_with_newline
    monkeypatch.setattr(journal, "_ends_with_newline", lambda p: calls.append(p) or real(p))
    for i in range(20):
        journal.append_event("cycle", {"i": i})
    assert len(calls) == 1


# --- read semantics ------------------------------------------------------------------------

def test_limit_zero_returns_nothing(jf):
    journal.append_event("cycle", {})
    journal.append_event("cycle", {})
    assert journal.read_events(limit=0) == []
    assert len(journal.read_events(limit=1)) == 1
    assert len(journal.read_events(limit=None)) == 2


def test_skipped_lines_are_counted_once_per_read(jf, caplog):
    jf.parent.mkdir(parents=True)
    jf.write_text('}\n2}\n{"ts": "t", "kind": "ok"}\n[1, 2]\n', encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        events = journal.read_events()
    assert [e["kind"] for e in events] == ["ok"]
    assert sum("skipped 3 malformed" in r.message for r in caplog.records) == 1


# --- rotation ------------------------------------------------------------------------------

def _all_ids(*paths):
    out = []
    for path in paths:
        if path.exists():
            out += [json.loads(l)["i"] for l in _lines(path)]
    return out


def _everything(active):
    return _all_ids(*journal.archive_files(), journal.previous_segment(), active)


def test_rotation_renames_segments_and_loses_nothing(jf, monkeypatch):
    monkeypatch.setenv("ZUGAMIND_JOURNAL_MAX_BYTES", "65536")   # the floor: 64 KB
    monkeypatch.setattr(journal, "_ROTATE_CHECK_EVERY", 50)
    for i in range(600):                                          # ~350 bytes each -> ~210 KB
        journal.append_event("cycle", {"i": i, "pad": "x" * 300})
    prev = journal.previous_segment()
    assert prev.exists() and journal.archive_files()
    assert _everything(jf) == list(range(600))                    # order kept, nothing lost, nothing duplicated
    if jf.exists():                                               # absent right after a rotation until the next append
        assert jf.stat().st_size <= 65536 + 50 * 400              # active bounded, up to one check-window of slack
    assert prev.stat().st_size <= 65536 + 50 * 400
    # readers see previous + active, oldest first
    seen = [e["i"] for e in journal.read_events(limit=None)]
    assert seen == _all_ids(prev, jf) and seen[-1] == 599 and len(seen) >= 100


def test_rotation_is_checked_every_n_appends_not_every_append(jf, monkeypatch):
    monkeypatch.setenv("ZUGAMIND_JOURNAL_MAX_BYTES", "65536")
    monkeypatch.setattr(journal, "_ROTATE_CHECK_EVERY", 1000)
    for i in range(300):
        journal.append_event("cycle", {"i": i, "pad": "x" * 300})
    assert not journal.previous_segment().exists()                # oversized, but the check has not fired


def test_rotation_failure_is_deferred_not_raised(jf, monkeypatch, caplog):
    monkeypatch.setenv("ZUGAMIND_JOURNAL_MAX_BYTES", "65536")
    monkeypatch.setattr(journal, "_ROTATE_CHECK_EVERY", 1000)
    for i in range(300):                                          # oversized, unrotated
        journal.append_event("cycle", {"i": i, "pad": "x" * 300})
    monkeypatch.setattr(journal, "_ROTATE_CHECK_EVERY", 1)

    def boom(src, dst):
        raise OSError("sharing violation")
    monkeypatch.setattr(journal.os, "rename", boom)
    with caplog.at_level(logging.WARNING):
        journal.append_event("cycle", {"i": 999})                 # must not raise
    assert any("rotation deferred" in r.message for r in caplog.records)
    assert len(_lines(jf)) == 301                                 # the event itself still landed


def test_appends_during_rotation_are_never_lost(jf, monkeypatch):
    """The first rotation snapshotted the file and replaced it, erasing any
    append that landed in between (caught in review 2026-08-28). A rename
    has no such window: an append goes to the fresh file or the renamed
    segment, both of which readers parse."""
    import threading
    monkeypatch.setenv("ZUGAMIND_JOURNAL_MAX_BYTES", "65536")
    monkeypatch.setattr(journal, "_ROTATE_CHECK_EVERY", 25)
    stop = threading.Event()
    counter = {"n": 0}
    lock = threading.Lock()

    def writer():
        while not stop.is_set():
            with lock:
                n = counter["n"]; counter["n"] += 1
            journal.append_event("cycle", {"i": n, "pad": "x" * 300})
    threads = [threading.Thread(target=writer) for _ in range(3)]
    for th in threads:
        th.start()
    import time
    time.sleep(1.5)
    stop.set()
    for th in threads:
        th.join()
    total = counter["n"]
    ids = sorted(_everything(jf))
    assert ids == list(range(total)), f"lost/duplicated: {len(ids)} ids for {total} appends"
    assert journal.previous_segment().exists()                    # rotation did happen
