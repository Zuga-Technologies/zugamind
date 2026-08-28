"""news_rss: conditional GET (2026-08-28).

Past the cache TTL each feed is re-fetched with If-None-Match /
If-Modified-Since from its last 200; a 304 reuses that feed's parsed items
and costs no body. No network: urlopen and _fetch are replaced in-process.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
from pathlib import Path

import pytest

_SCANNER_DIR = Path(__file__).resolve().parent.parent.parent / "examples" / "custom-scanners"
if str(_SCANNER_DIR) not in sys.path:
    sys.path.insert(0, str(_SCANNER_DIR))

import news_rss  # noqa: E402

HOUR = 3600.0
ETAG_V1, ETAG_V2 = '"v1"', '"v2"'
LM_V1 = "Mon, 13 Aug 2026 09:00:00 GMT"
LM_V2 = "Tue, 14 Aug 2026 09:00:00 GMT"


class _Resp:
    """The slice of an urlopen response _fetch touches."""

    def __init__(self, body: bytes, headers: dict[str, str]):
        self._body, self.headers = body, headers

    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# --------------------------------------------------------------------------
# _fetch: the HTTP half
# --------------------------------------------------------------------------

def test_fetch_sends_validators_and_captures_the_new_ones(monkeypatch):
    sent = {}

    def fake_urlopen(req, timeout=None):
        sent.update({k.lower(): v for k, v in req.header_items()})
        return _Resp(b"<rss/>", {"ETag": ETAG_V2, "Last-Modified": LM_V2})
    monkeypatch.setattr(news_rss.urllib.request, "urlopen", fake_urlopen)

    status, body, validators = news_rss._fetch("https://f/rss", {"etag": ETAG_V1, "last_modified": LM_V1})
    assert (status, body) == ("ok", b"<rss/>")
    assert sent["if-none-match"] == ETAG_V1
    assert sent["if-modified-since"] == LM_V1
    assert validators == {"etag": ETAG_V2, "last_modified": LM_V2}


def test_fetch_without_validators_sends_none(monkeypatch):
    sent = {}

    def fake_urlopen(req, timeout=None):
        sent.update({k.lower(): v for k, v in req.header_items()})
        return _Resp(b"<rss/>", {})
    monkeypatch.setattr(news_rss.urllib.request, "urlopen", fake_urlopen)

    assert news_rss._fetch("https://f/rss") == ("ok", b"<rss/>", {})
    assert "if-none-match" not in sent and "if-modified-since" not in sent


def test_fetch_304_is_not_modified_not_a_failure(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, None)
    monkeypatch.setattr(news_rss.urllib.request, "urlopen", fake_urlopen)
    assert news_rss._fetch("https://f/rss", {"etag": ETAG_V1}) == ("not_modified", None, {})


def test_fetch_other_http_errors_still_fail(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 503, "Unavailable", {}, None)
    monkeypatch.setattr(news_rss.urllib.request, "urlopen", fake_urlopen)
    assert news_rss._fetch("https://f/rss", {"etag": ETAG_V1}) == ("failed", None, {})


# --------------------------------------------------------------------------
# scan: the cache half
# --------------------------------------------------------------------------

FEED_URL = "https://feed.invalid/rss"


def _feed(items):
    body = "".join(f"<item><title>{t}</title><link>{l}</link></item>" for t, l in items)
    return f"<rss><channel>{body}</channel></rss>".encode()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(news_rss, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(news_rss, "_FETCH_CACHE_FILE", tmp_path / "fetch.json")
    monkeypatch.setattr(news_rss, "_SEEN_FILE", tmp_path / "seen.json")
    monkeypatch.setenv("ZUGAMIND_NEWS_FEEDS", FEED_URL)
    monkeypatch.setenv("ZUGAMIND_NEWS_CACHE_TTL", "-1")  # re-fetch every call
    monkeypatch.delenv("ZUGAMIND_NEWS_KEYWORDS", raising=False)
    return tmp_path


def test_scan_sends_last_validators_and_reuses_items_on_304(env, monkeypatch):
    calls: list[dict | None] = []
    script = [
        ("ok", _feed([("One", "https://x/1")]), {"etag": ETAG_V1}),
        ("not_modified", None, {}),
        ("ok", _feed([("One", "https://x/1"), ("Two", "https://x/2")]), {"etag": ETAG_V2}),
    ]

    def fake_fetch(url, validators=None):
        calls.append(validators)
        return script[len(calls) - 1]
    monkeypatch.setattr(news_rss, "_fetch", fake_fetch)

    assert news_rss.scan_news_rss() == []  # cold start baselines "One"
    assert calls[0] is None

    assert news_rss.scan_news_rss() == []  # 304: "One" reused, still seen
    assert calls[1] == {"etag": ETAG_V1}
    cache = json.loads((env / "fetch.json").read_text(encoding="utf-8"))
    assert cache["feeds"][FEED_URL]["validators"] == {"etag": ETAG_V1}
    assert [i["title"] for i in cache["feeds"][FEED_URL]["items"]] == ["One"]
    assert [i["title"] for i in cache["items"]] == ["One"]

    third = news_rss.scan_news_rss()  # changed feed: fresh body, new validators
    assert calls[2] == {"etag": ETAG_V1}
    assert [t["title"] for t in third] == ["Two"]
    cache = json.loads((env / "fetch.json").read_text(encoding="utf-8"))
    assert cache["feeds"][FEED_URL]["validators"] == {"etag": ETAG_V2}


def test_legacy_cache_without_feeds_fetches_unconditionally(env, monkeypatch):
    (env / "fetch.json").write_text(json.dumps({"ts": 0, "items": [
        {"source": FEED_URL, "title": "Old", "link": "https://x/old", "summary": "", "guid": ""}]}), encoding="utf-8")
    (env / "seen.json").write_text(json.dumps({"https://x/old": time.time()}), encoding="utf-8")
    calls = []

    def fake_fetch(url, validators=None):
        calls.append(validators)
        return ("ok", _feed([("Old", "https://x/old")]), {"etag": ETAG_V1})
    monkeypatch.setattr(news_rss, "_fetch", fake_fetch)

    news_rss.scan_news_rss()
    assert calls == [None]
    cache = json.loads((env / "fetch.json").read_text(encoding="utf-8"))
    assert cache["feeds"][FEED_URL]["validators"] == {"etag": ETAG_V1}


def test_304_with_nothing_cached_is_treated_as_a_miss(env, monkeypatch):
    """Validators are only sent when there are items to fall back on, so this
    can only happen if a server 304s unprompted — it must not crash."""
    monkeypatch.setattr(news_rss, "_fetch", lambda url, validators=None: ("not_modified", None, {}))
    assert news_rss.scan_news_rss() == []
