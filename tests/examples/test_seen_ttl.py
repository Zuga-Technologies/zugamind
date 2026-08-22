"""Seen-set TTL eviction in the example scanners (news_rss / x_activity /
discord_activity): {id: last_seen_epoch} with refresh-on-observe + TTL
eviction (by age, never by count) replaced the old alphabetical
``sorted(ids)[-N:]`` trim. Legacy bare-list files must still load.

Same sys.path setup as test_agent_reach.py; no network is touched — only
the pure load/save helpers are exercised.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_SCANNER_DIR = Path(__file__).resolve().parent.parent.parent / "examples" / "custom-scanners"
if str(_SCANNER_DIR) not in sys.path:
    sys.path.insert(0, str(_SCANNER_DIR))

import news_rss  # noqa: E402
import x_activity  # noqa: E402
import discord_activity  # noqa: E402


def test_news_rss_legacy_list_loads_as_just_seen(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text(json.dumps(["https://a", "https://b"]), encoding="utf-8")
    seen = news_rss._load_seen(path)
    assert set(seen) == {"https://a", "https://b"}
    assert all(isinstance(v, float) for v in seen.values())


def test_news_rss_ttl_evicts_only_ids_absent_past_ttl(tmp_path):
    path = tmp_path / "seen.json"
    now = 1_800_000_000.0
    ttl = news_rss._SEEN_TTL_SECONDS
    seen = {
        "https://fresh": now,               # observed just now
        "https://kept": now - ttl + 60,     # inside the window
        "https://gone": now - ttl - 60,     # absent longer than the TTL
    }
    news_rss._save_seen(path, seen, now)
    assert set(news_rss._load_seen(path)) == {"https://fresh", "https://kept"}


def test_news_rss_backstop_cap_evicts_oldest_first_not_alphabetical(tmp_path, monkeypatch):
    path = tmp_path / "seen.json"
    monkeypatch.setattr(news_rss, "_SEEN_MAX", 2)
    now = 1_800_000_000.0
    # Alphabetically "a" would survive a sorted()[-N:] trim; by age it is the
    # OLDEST and must be the one evicted.
    seen = {"https://a": now - 300, "https://z": now - 200, "https://m": now - 100}
    news_rss._save_seen(path, seen, now)
    assert set(news_rss._load_seen(path)) == {"https://z", "https://m"}


def test_x_activity_and_discord_share_the_same_semantics(tmp_path, monkeypatch):
    now = 1_800_000_000.0
    # x_activity: same helper shape as news_rss
    p = tmp_path / "x.json"
    x_activity._save_seen(p, {"1": now, "2": now - x_activity._SEEN_TTL_SECONDS - 1}, now)
    assert set(x_activity._load_seen(p)) == {"1"}
    # discord_activity: module-level file path
    monkeypatch.setattr(discord_activity, "_SEEN_FILE", tmp_path / "d.json")
    monkeypatch.setattr(discord_activity, "_CACHE_DIR", tmp_path)
    (tmp_path / "d.json").write_text(json.dumps(["legacy-id"]), encoding="utf-8")
    seen = discord_activity._load_seen()
    assert set(seen) == {"legacy-id"}
    seen["new-id"] = now
    seen["legacy-id"] = now - discord_activity._SEEN_TTL_SECONDS - 1
    discord_activity._save_seen(seen, now)
    assert set(discord_activity._load_seen()) == {"new-id"}
