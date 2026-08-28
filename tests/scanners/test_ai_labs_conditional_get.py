"""ai_labs: conditional GET (2026-08-28) — same contract as the news_rss
example. Past `_CACHE_TTL` each feed is re-fetched with If-None-Match /
If-Modified-Since from its last 200 and a 304 reuses that feed's parsed
items. No network: _fetch / urlopen are replaced in-process.
"""
from __future__ import annotations

import json
import time
import urllib.error

import pytest

import scanners.world.ai_labs as ai_labs

ETAG = '"abc"'


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "scanner_cache"
    d.mkdir(parents=True)
    monkeypatch.setattr(ai_labs, "_CACHE_DIR", d)
    return d


def _item(lab="anthropic", slug="a", title="A post"):
    return {"lab": lab, "title": title, "summary": "",
            "link": f"https://example.invalid/{lab}/{slug}", "published": time.time()}


def test_fetch_304_is_not_modified(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, None)
    monkeypatch.setattr(ai_labs.urllib.request, "urlopen", fake_urlopen)
    assert ai_labs._fetch("https://f/rss", {"etag": ETAG}) == ("not_modified", None, {})


def test_read_cache_ignore_ttl_returns_a_stale_cache(cache_dir):
    ai_labs._write_cache({"ts": time.time() - 10 * ai_labs._CACHE_TTL, "items": [_item()]})
    assert ai_labs._read_cache() is None
    assert ai_labs._read_cache(ignore_ttl=True)["items"][0]["title"] == "A post"


def test_stale_cache_validators_are_sent_and_items_reused_on_304(cache_dir, monkeypatch):
    lab, url, _fmt = ai_labs._FEEDS[0]
    kept = [_item(lab=lab, slug="kept", title="Kept")]
    ai_labs._write_cache({"ts": time.time() - 10 * ai_labs._CACHE_TTL, "items": kept,
                          "feeds": {url: {"validators": {"etag": ETAG}, "items": kept}}})
    calls: dict[str, dict | None] = {}

    def fake_fetch(u, validators=None):
        calls[u] = validators
        return ("not_modified", None, {}) if u == url else ("failed", None, {})
    monkeypatch.setattr(ai_labs, "_fetch", fake_fetch)

    ai_labs.scan_ai_labs()

    assert calls[url] == {"etag": ETAG}
    assert all(v is None for u, v in calls.items() if u != url)  # nothing cached for the rest
    cache = json.loads(ai_labs._cache_file().read_text())
    assert cache["feeds"][url]["validators"] == {"etag": ETAG}
    assert [i["title"] for i in cache["feeds"][url]["items"]] == ["Kept"]
    assert [i["title"] for i in cache["items"]] == ["Kept"]
    assert time.time() - cache["ts"] < 60  # the cache is fresh again after a 304


def test_fresh_200_replaces_validators_and_items(cache_dir, monkeypatch):
    lab, url, _fmt = next(f for f in ai_labs._FEEDS if f[2] == "rss")  # _FEEDS[0] is a scraped HTML page
    ai_labs._write_cache({"ts": 0, "items": [], "feeds": {url: {"validators": {"etag": ETAG}, "items": [_item()]}}})
    rss = b"""<rss><channel><item><title>Fresh</title><link>https://x/fresh</link>
              <pubDate>Tue, 14 Aug 2026 09:00:00 +0000</pubDate></item></channel></rss>""".decode()

    def fake_fetch(u, validators=None):
        return ("ok", rss, {"etag": '"new"'}) if u == url else ("failed", None, {})
    monkeypatch.setattr(ai_labs, "_fetch", fake_fetch)

    ai_labs.scan_ai_labs()
    cache = json.loads(ai_labs._cache_file().read_text())
    assert cache["feeds"][url]["validators"] == {"etag": '"new"'}
    assert [i["title"] for i in cache["feeds"][url]["items"]] == ["Fresh"]
