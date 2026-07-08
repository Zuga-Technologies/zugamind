"""Tests for the hackernews scanner disk cache.

Proof case: without caching, the scanner makes 1 topstories + 30 item = 31
uncached HTTP calls every cycle. With caching, a warm second cycle should
make ~0 fetches, and a single new entrant should cost exactly one item
fetch. All HTTP is mocked — no live network calls in this suite.
"""
from __future__ import annotations

from zugamind.scanners.world import hackernews as hn


def fake_hn(tmp_path, monkeypatch):
    monkeypatch.setattr(hn, "_CACHE_PATH", tmp_path / "hackernews.json")
    top_ids = list(range(1, 31))
    calls = {"top": 0, "item": 0}

    def fake_fetch(url):
        if url == hn._TOP_URL:
            calls["top"] += 1
            return list(top_ids)
        calls["item"] += 1
        sid = int(url.split("/item/")[1].split(".json")[0])
        return {"title": f"AI story {sid}", "url": f"http://x/{sid}", "score": sid}

    monkeypatch.setattr(hn, "_fetch_json", fake_fetch)
    return top_ids, calls


def test_cold_cycle_fetches_everything(tmp_path, monkeypatch):
    _, calls = fake_hn(tmp_path, monkeypatch)
    monkeypatch.setattr(hn.time, "time", lambda: 1000.0)
    out = hn.scan_hackernews()
    assert len(out) == 30
    assert calls["top"] == 1
    assert calls["item"] == 30


def test_warm_cycle_makes_no_fetches(tmp_path, monkeypatch):
    _, calls = fake_hn(tmp_path, monkeypatch)
    monkeypatch.setattr(hn.time, "time", lambda: 1000.0)
    hn.scan_hackernews()
    calls["top"] = calls["item"] = 0
    monkeypatch.setattr(hn.time, "time", lambda: 1010.0)
    out = hn.scan_hackernews()
    assert len(out) == 30
    assert calls["top"] == 0
    assert calls["item"] == 0


def test_only_new_entrant_is_fetched(tmp_path, monkeypatch):
    top_ids, calls = fake_hn(tmp_path, monkeypatch)
    monkeypatch.setattr(hn.time, "time", lambda: 1000.0)
    hn.scan_hackernews()
    top_ids[:] = [999] + top_ids[:-1]
    calls["top"] = calls["item"] = 0
    monkeypatch.setattr(hn.time, "time", lambda: 1000.0 + hn._TOP_TTL + 1)
    hn.scan_hackernews()
    assert calls["top"] == 1
    assert calls["item"] == 1


def test_corrupt_cache_falls_back_to_live(tmp_path, monkeypatch):
    _, calls = fake_hn(tmp_path, monkeypatch)
    hn._CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    hn._CACHE_PATH.write_text("{ not json", "utf-8")
    monkeypatch.setattr(hn.time, "time", lambda: 1000.0)
    out = hn.scan_hackernews()
    assert len(out) == 30
    assert calls["item"] == 30
