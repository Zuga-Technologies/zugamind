"""Example private scanner — general news via RSS/Atom.

Not part of the ZugaMind package; a worked example of the extra_scanners
pattern documented in examples/custom-scanners/README.md. Copy this file
into your own deployment and pass it to
StreamRunner(extra_scanners={"scan_news_rss": scan_news_rss}).

Watches ANY RSS/Atom feeds you configure (wire outlets, industry trades,
a competitor's blog, whatever matters to your project) and turns unseen
items into triggers. This is the general-purpose sibling of the shipped
`scan_ai_labs` scanner (scanners/world/ai_labs.py) — same stdlib RSS
parsing approach, but pointed at feeds YOU choose instead of a fixed
curated AI-lab list.

Honesty note on "real time": RSS is not a push feed. Most outlets publish
within minutes of going live, and this scanner's own cache TTL controls
how often it re-checks — set ZUGAMIND_NEWS_CACHE_TTL low (e.g. 300s) for
near-real-time, but it is still poll-based, not an instant push. For truly
sub-minute latency on a specific source you'd need that source's own
push/webhook API, which is source-specific and out of scope for a generic
scanner like this one.

Configuration (env):
    ZUGAMIND_NEWS_FEEDS       comma-separated RSS/Atom URLs. Required —
                              unset means the scanner is off, returns [].
    ZUGAMIND_NEWS_CACHE_TTL   seconds between re-fetching the feed list.
                              Default 600 (10 min). Lower = fresher, more
                              requests against the source's server — be a
                              good citizen, most outlets rate-limit or
                              block aggressive polling.
    ZUGAMIND_NEWS_KEYWORDS    optional comma-separated keywords, case-
                              insensitive. If set, only items whose title
                              or summary contains at least one keyword
                              trigger — everything else is still fetched
                              (for dedupe bookkeeping) but filtered out.
                              Unset = every new item triggers.

Dedupe is "seen link" persisted to disk, same pattern as the other
examples in this directory — once an item has triggered it will not
trigger again, even across restarts, even if it stays in the feed's
recent-items window on the next fetch.

Stdlib only (urllib.request, xml.etree.ElementTree). Fail-silent per
scanner contract — one broken feed URL does not sink the others.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

logger = logging.getLogger("zugamind.examples.news_rss")

_TIMEOUT = 8.0
_DEFAULT_CACHE_TTL = 600
_MAX_TRIGGERS = 5
_DATA_DIR = Path(os.environ.get("ZUGAMIND_DATA_DIR") or Path(__file__).resolve().parent / "data")
_CACHE_DIR = _DATA_DIR / "scanner_cache"
_FETCH_CACHE_FILE = _CACHE_DIR / "news_rss_fetch.json"
_SEEN_FILE = _CACHE_DIR / "news_rss_seen.json"


def _load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("news_rss cache load failed (%s): %s", path.name, e)
    return default


def _save_json(path: Path, payload: Any) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as e:
        logger.debug("news_rss cache save failed (%s): %s", path.name, e)


def _fetch(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ZugaMind/example-scanner"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        logger.debug("news_rss fetch %s failed: %s", url, e)
        return None


def _parse_feed(xml_text: str, source: str) -> list[dict[str, str]]:
    """Same tag-agnostic RSS/Atom walk as scanners/world/ai_labs.py."""
    items: list[dict[str, str]] = []
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return items
    for it in root.iter():
        tag = it.tag.lower().split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        title, link, summary = "", "", ""
        for child in it:
            ctag = child.tag.lower().split("}")[-1]
            txt = (child.text or "").strip()
            if ctag == "title":
                title = txt
            elif ctag == "link":
                link = child.attrib.get("href") or txt
            elif ctag in ("description", "summary"):
                summary = re.sub(r"<[^>]+>", "", txt)[:300]
        if title and link:
            items.append({"source": source, "title": title[:200], "link": link, "summary": summary})
        if len(items) >= 10:
            break
    return items


def scan_news_rss() -> list[dict[str, Any]]:
    """Return `news_rss` triggers for unseen items across the configured feeds."""
    feeds_raw = os.environ.get("ZUGAMIND_NEWS_FEEDS", "").strip()
    if not feeds_raw:
        return []
    feed_urls = [u.strip() for u in feeds_raw.split(",") if u.strip()]
    if not feed_urls:
        return []

    ttl = int(os.environ.get("ZUGAMIND_NEWS_CACHE_TTL", str(_DEFAULT_CACHE_TTL)))
    keywords = [k.strip().lower() for k in
                os.environ.get("ZUGAMIND_NEWS_KEYWORDS", "").split(",") if k.strip()]

    fetch_cache = _load_json(_FETCH_CACHE_FILE, {"ts": 0, "items": []})
    if time.time() - fetch_cache.get("ts", 0) > ttl:
        items: list[dict[str, str]] = []
        for url in feed_urls:
            txt = _fetch(url)
            if not txt:
                continue
            items.extend(_parse_feed(txt, url))
        fetch_cache = {"ts": time.time(), "items": items}
        _save_json(_FETCH_CACHE_FILE, fetch_cache)

    seen: set[str] = set(_load_json(_SEEN_FILE, []))
    triggers: list[dict[str, Any]] = []
    newly_seen: set[str] = set()

    for it in fetch_cache.get("items", []):
        link = it.get("link", "")
        if not link or link in seen:
            continue
        newly_seen.add(link)
        text = f"{it.get('title', '')} {it.get('summary', '')}".lower()
        if keywords and not any(kw in text for kw in keywords):
            continue
        detail = it["title"]
        if it.get("summary"):
            detail += " -- " + it["summary"][:160]
        triggers.append({
            "type": "news_rss",
            "detail": detail[:380],
            "source": it.get("source", ""),
            "title": it.get("title", ""),
            "link": link,
            "novelty": 0.8,
            "relevance": 0.6,
            "urgency": 0.3,
        })
        if len(triggers) >= _MAX_TRIGGERS:
            break

    if newly_seen:
        merged = seen | newly_seen
        _save_json(_SEEN_FILE, sorted(merged)[-1000:])

    return triggers
