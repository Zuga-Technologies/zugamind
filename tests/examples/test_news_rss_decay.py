"""news_rss: urgency decays with publish age and new items emit newest-first
(ported from scanners/world/ai_labs.py, 2026-08-28).

A constant urgency made a year-old item bid identically to this morning's,
and feed order plus a hard per-sweep cap let a pinned top-slot item crowd
fresh stories out. No network: `_fetch` is replaced by an in-memory feed.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SCANNER_DIR = Path(__file__).resolve().parent.parent.parent / "examples" / "custom-scanners"
if str(_SCANNER_DIR) not in sys.path:
    sys.path.insert(0, str(_SCANNER_DIR))

import news_rss  # noqa: E402

HOUR = 3600.0
DAY = 24 * HOUR


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------

@pytest.mark.parametrize("age_h,expected", [
    (0.0, 0.3),
    (23.0, 0.3),
    (48.0, 0.15),
    (72.0, 0.0),
    (25 * 24, 0.0),
])
def test_urgency_decays_with_publish_age(age_h, expected):
    now = time.time()
    assert news_rss._urgency_for(now - age_h * HOUR, now) == pytest.approx(expected)


def test_unknown_publish_date_fails_open_at_fresh():
    """A feed that stops exposing dates must not go silent."""
    assert news_rss._urgency_for(None, time.time()) == 0.3


def test_parse_feed_reads_rss_pubdate_and_atom_updated():
    rss = b"""<rss><channel>
      <item><title>Dated</title><link>https://x/1</link>
            <pubDate>Tue, 14 Aug 2026 09:00:00 +0000</pubDate></item>
      <item><title>Undated</title><link>https://x/2</link></item>
    </channel></rss>"""
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>Atom</title><link href="https://x/3"/>
             <updated>2026-08-14T09:00:00Z</updated></entry>
    </feed>"""
    by_title = {i["title"]: i for i in news_rss._parse_feed(rss, "rss") + news_rss._parse_feed(atom, "atom")}
    expected = datetime(2026, 8, 14, 9, 0, 0, tzinfo=timezone.utc).timestamp()
    assert by_title["Dated"]["published"] == pytest.approx(expected)
    assert by_title["Atom"]["published"] == pytest.approx(expected)
    assert by_title["Undated"]["published"] is None


# --------------------------------------------------------------------------
# end to end, no network
# --------------------------------------------------------------------------

def _feed(items):
    """items: (title, link, published_epoch|None) -> RSS bytes, in that order."""
    body = ""
    for title, link, published in items:
        date = ""
        if published is not None:
            date = f"<pubDate>{time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime(published))}</pubDate>"
        body += f"<item><title>{title}</title><link>{link}</link>{date}</item>"
    return f"<rss><channel>{body}</channel></rss>".encode()


@pytest.fixture()
def scanner(tmp_path, monkeypatch):
    monkeypatch.setattr(news_rss, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(news_rss, "_FETCH_CACHE_FILE", tmp_path / "fetch.json")
    monkeypatch.setattr(news_rss, "_SEEN_FILE", tmp_path / "seen.json")
    monkeypatch.setenv("ZUGAMIND_NEWS_FEEDS", "https://feed.invalid/rss")
    monkeypatch.setenv("ZUGAMIND_NEWS_CACHE_TTL", "-1")  # re-fetch every call
    monkeypatch.delenv("ZUGAMIND_NEWS_KEYWORDS", raising=False)
    state = {"items": []}
    monkeypatch.setattr(news_rss, "_fetch", lambda url, validators=None: ("ok", _feed(state["items"]), {}))

    def run(items):
        state["items"] = items
        return news_rss.scan_news_rss()
    return run


def test_fresh_item_beats_pinned_stale_item_for_the_cap(scanner, monkeypatch):
    now = time.time()
    monkeypatch.setattr(news_rss, "_MAX_TRIGGERS", 1)
    assert scanner([("Baseline", "https://x/base", now - HOUR)]) == []  # cold start baselines

    feed = [("Stale pinned", "https://x/stale", now - 25 * DAY),     # first in feed order
            ("Fresh", "https://x/fresh", now - HOUR),
            ("Baseline", "https://x/base", now - HOUR)]
    first = scanner(feed)
    assert [t["title"] for t in first] == ["Fresh"]
    assert first[0]["urgency"] == 0.3
    assert first[0]["published"] == pytest.approx(now - HOUR)

    # The item past the cap was NOT stamped: it fires on the next sweep,
    # and by then its age prices it at zero urgency.
    second = scanner(feed)
    assert [t["title"] for t in second] == ["Stale pinned"]
    assert second[0]["urgency"] == 0.0
    assert scanner(feed) == []


def test_cap_no_longer_skips_refresh_on_observe_for_later_items(scanner, monkeypatch):
    """The old single loop broke out at the cap before reaching items later in
    the feed, so a still-live item could go un-refreshed and age past the TTL."""
    now = time.time()
    monkeypatch.setattr(news_rss, "_MAX_TRIGGERS", 1)
    scanner([("Baseline", "https://x/base", now - HOUR)])
    stale_stamp = now - 20 * DAY
    seen_path = news_rss._SEEN_FILE
    seen_path.write_text(json.dumps({"https://x/base": stale_stamp}), encoding="utf-8")

    before = time.time()
    scanner([("New A", "https://x/a", now - HOUR),
             ("New B", "https://x/b", now - 2 * HOUR),
             ("Baseline", "https://x/base", now - HOUR)])  # after the cap in feed order
    seen = json.loads(seen_path.read_text(encoding="utf-8"))
    assert seen["https://x/base"] >= before


def test_keyword_rejected_items_are_stamped_and_never_fire(scanner, monkeypatch):
    now = time.time()
    monkeypatch.setenv("ZUGAMIND_NEWS_KEYWORDS", "rocket")
    scanner([("Baseline", "https://x/base", now - HOUR)])
    assert scanner([("Baseline", "https://x/base", now - HOUR),
                    ("Kittens", "https://x/k", now - HOUR),
                    ("Rocket launch", "https://x/r", now - HOUR)]) and True
    seen = json.loads(news_rss._SEEN_FILE.read_text(encoding="utf-8"))
    assert "https://x/k" in seen
    assert scanner([("Kittens", "https://x/k", now - HOUR)]) == []
