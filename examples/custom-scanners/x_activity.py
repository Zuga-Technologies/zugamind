"""Example private scanner — X (Twitter) recent-post search.

Not part of the ZugaMind package; a worked example of the extra_scanners
pattern documented in examples/custom-scanners/README.md. Copy this file
into your own deployment and pass it to
StreamRunner(extra_scanners={"scan_x_activity": scan_x_activity}).

Watches X's recent-search endpoint for posts matching a query you
configure (keywords, an account, a hashtag) and turns unseen ones into
triggers.

Cost note, read before configuring: X API v2 is pay-per-use as of 2026
(no more flat-fee tiers) at roughly $0.005 per post read. This scanner
is deliberately tuned for a LOW-cost default — a 60 minute cache TTL and
a small per-poll result count — not maximum freshness. At the defaults
below (60 min, 15 results/poll) that's ~24 polls/day x 15 = 360
reads/day, ~10,800/month, ~$54/month. Tighten `ZUGAMIND_X_CACHE_TTL`
lower or `ZUGAMIND_X_MAX_RESULTS` higher only with the cost math redone —
both scale the bill roughly linearly.

Configuration (env):
    X_BEARER_TOKEN         App-only bearer token (developer.x.com).
    ZUGAMIND_X_QUERY       search query, X search syntax (e.g.
                           "from:someaccount" or "\"exact phrase\" -is:retweet").
                           Required — unset means the scanner is off.
    ZUGAMIND_X_MAX_RESULTS  results per poll. Default 15 (X API minimum is
                            10, max 100 per call) — kept low by design,
                            see cost note above.
    ZUGAMIND_X_CACHE_TTL    seconds between polls. Default 3600 (60 min) —
                            kept high by design; this is meant to catch
                            things worth knowing about within the hour,
                            not sub-minute breaking news.

Dedupe is "seen post id" persisted to disk, same pattern as the other
examples in this directory.

Stdlib only (urllib.request). Fail-silent per scanner contract.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger("zugamind.examples.x_activity")

_TIMEOUT = 8.0
_DEFAULT_CACHE_TTL = 3600
_DEFAULT_MAX_RESULTS = 15
_MAX_TRIGGERS = 5
_DATA_DIR = Path(os.environ.get("ZUGAMIND_DATA_DIR") or Path(__file__).resolve().parent / "data")
_CACHE_DIR = _DATA_DIR / "scanner_cache"
_FETCH_CACHE_FILE = _CACHE_DIR / "x_activity_fetch.json"
_SEEN_FILE = _CACHE_DIR / "x_activity_seen.json"
_API = "https://api.x.com/2/tweets/search/recent"


def _load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("x_activity cache load failed (%s): %s", path.name, e)
    return default


def _save_json(path: Path, payload: Any) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as e:
        logger.debug("x_activity cache save failed (%s): %s", path.name, e)


def _fetch_recent(query: str, token: str, max_results: int) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode({
        "query": query,
        "max_results": max(10, min(max_results, 100)),
        "tweet.fields": "created_at,author_id",
    })
    url = f"{_API}?{params}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ZugaMind/example-scanner",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    if "errors" in data and "data" not in data:
        logger.debug("x_activity API error: %s", data.get("errors"))
        return []
    return data.get("data", [])


def scan_x_activity() -> list[dict[str, Any]]:
    """Return `x_post` triggers for unseen posts matching the configured query."""
    token = os.environ.get("X_BEARER_TOKEN", "").strip()
    query = os.environ.get("ZUGAMIND_X_QUERY", "").strip()
    if not token or not query:
        return []

    ttl = int(os.environ.get("ZUGAMIND_X_CACHE_TTL", str(_DEFAULT_CACHE_TTL)))
    max_results = int(os.environ.get("ZUGAMIND_X_MAX_RESULTS", str(_DEFAULT_MAX_RESULTS)))

    fetch_cache = _load_json(_FETCH_CACHE_FILE, {"ts": 0, "posts": []})
    if time.time() - fetch_cache.get("ts", 0) > ttl:
        try:
            posts = _fetch_recent(query, token, max_results)
        except urllib.error.HTTPError as e:
            logger.debug("x_activity fetch failed: HTTP %s", e.code)
            posts = fetch_cache.get("posts", [])
        except Exception as e:
            logger.debug("x_activity fetch failed: %s", e)
            posts = fetch_cache.get("posts", [])
        else:
            fetch_cache = {"ts": time.time(), "posts": posts}
            _save_json(_FETCH_CACHE_FILE, fetch_cache)

    seen: set[str] = set(_load_json(_SEEN_FILE, []))
    triggers: list[dict[str, Any]] = []
    newly_seen: set[str] = set()

    for post in fetch_cache.get("posts", []):
        pid = post.get("id")
        if not pid or pid in seen:
            continue
        newly_seen.add(pid)
        text = (post.get("text") or "")[:250]
        triggers.append({
            "type": "x_post",
            "detail": f"x.com post: {text}",
            "post_id": pid,
            "author_id": post.get("author_id", ""),
            "created_at": post.get("created_at", ""),
            "url": f"https://x.com/i/web/status/{pid}",
            "novelty": 0.75,
            "relevance": 0.6,
            "urgency": 0.3,
        })
        if len(triggers) >= _MAX_TRIGGERS:
            break

    if newly_seen:
        merged = seen | newly_seen
        _save_json(_SEEN_FILE, sorted(merged)[-1000:])

    return triggers
