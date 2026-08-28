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

Dedupe is "seen id" persisted to disk, same pattern as the other examples
in this directory — once an item has triggered it will not trigger again,
even across restarts, even if it stays in the feed's recent-items window
on the next fetch. Identity prefers the feed's own <guid>/<id> when present
and falls back to <link> only when it isn't — a bare link is unstable for
feeds that append tracking/session query params, which would otherwise
re-fire the same story every time its URL happened to change. First run
(no seen-set on disk yet) baselines the current feed contents silently
instead of announcing a feed's entire back-catalogue as breaking news.

Stdlib only (urllib.request, xml.etree.ElementTree). Fail-silent per
scanner contract — one broken feed URL does not sink the others.
"""

from __future__ import annotations

import html
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
_MAX_FEED_BYTES = 3 * 1024 * 1024  # a real RSS/Atom feed is KBs; caps memory/CPU cost of a hostile or runaway response
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
    """Atomic write (tmp + replace) — a fetch cache or seen-set torn mid-write
    by a killed process is worse than a stale one; the TTL/cadence gates
    elsewhere mean a lost write just costs one extra fetch/re-check next cycle."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        logger.debug("news_rss cache save failed (%s): %s", path.name, e)


# Seen-set eviction (changed 2026-08-22): the seen-set used to be a bare id
# list trimmed to `sorted(ids)[-1000:]` — which evicts ALPHABETICALLY, not by
# age, so for URLs the 1000-cap threw away arbitrary entries and a still-live
# feed item could be re-announced. It's now {id: last_seen_epoch}: every id
# observed in a fetch is re-stamped (refresh-on-observe), and an id is evicted
# only after it has been ABSENT from fetches for _SEEN_TTL_SECONDS — so a
# slow feed that keeps an item around for months can never see it re-fire,
# while genuinely gone items age out. TTL-not-count is the same policy the
# widely-cited production precedent uses (Stripe's idempotency keys: pruned
# by age, never by count). _SEEN_MAX stays only as a memory backstop and
# evicts OLDEST-first. Legacy bare-list files load fine (treated as "seen
# just now"), so upgrading in place is safe.
_SEEN_TTL_SECONDS = 30 * 86400
_SEEN_MAX = 5000


def _load_seen(path: Path) -> dict[str, float]:
    raw = _load_json(path, {})
    now = time.time()
    if isinstance(raw, list):  # legacy format: bare id list
        return {str(i): now for i in raw}
    if isinstance(raw, dict):
        out: dict[str, float] = {}
        for k, v in raw.items():
            try:
                out[str(k)] = float(v)
            except (TypeError, ValueError):
                out[str(k)] = now
        return out
    return {}


def _save_seen(path: Path, seen: dict[str, float], now: float) -> None:
    cutoff = now - _SEEN_TTL_SECONDS
    kept = {k: v for k, v in seen.items() if v >= cutoff}
    if len(kept) > _SEEN_MAX:
        kept = dict(sorted(kept.items(), key=lambda kv: kv[1])[-_SEEN_MAX:])
    _save_json(path, kept)


def _fetch(url: str) -> bytes | None:
    """Raw bytes, deliberately not pre-decoded — _parse_feed hands them to
    ElementTree as-is so expat can honor whatever encoding the feed's own XML
    declaration claims. A blind .decode("utf-8") here would silently mangle
    or drop text on a feed whose HTTP Content-Type charset lies (or is just
    absent), which is common enough in the wild not to assume UTF-8 upfront.
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ZugaMind/example-scanner"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = resp.read(_MAX_FEED_BYTES + 1)
        if len(data) > _MAX_FEED_BYTES:
            logger.debug("news_rss fetch %s exceeded %d bytes, skipping", url, _MAX_FEED_BYTES)
            return None
        return data
    except Exception as e:
        logger.debug("news_rss fetch %s failed: %s", url, e)
        return None


def _parse_feed(xml_bytes: bytes, source: str) -> list[dict[str, str]]:
    """Same tag-agnostic RSS/Atom walk as scanners/world/ai_labs.py.

    Takes raw bytes, not text — see _fetch's docstring on why decoding is
    left to ElementTree/expat instead of being done upfront.
    """
    items: list[dict[str, str]] = []
    # RSS/Atom never legitimately declares a DOCTYPE. A hostile feed could use
    # one to define nested internal entities (a "billion laughs" expansion
    # bomb) that blow up CPU/memory during parsing — xml.etree.ElementTree
    # does not guard against this itself. DOCTYPE, if present at all, must
    # precede the root element, so sniffing a prefix is enough; this is a
    # blunt reject-the-feed check, not a general XML sanitizer.
    if b"<!DOCTYPE" in xml_bytes[:4096]:
        logger.debug("news_rss feed %s has a DOCTYPE, skipping (unexpected for RSS/Atom)", source)
        return items
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return items
    for it in root.iter():
        tag = it.tag.lower().split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        title, link, summary, guid = "", "", "", ""
        for child in it:
            ctag = child.tag.lower().split("}")[-1]
            txt = (child.text or "").strip()
            if ctag == "title":
                title = txt
            elif ctag == "link":
                link = child.attrib.get("href") or txt
            elif ctag in ("description", "summary"):
                summary = html.unescape(re.sub(r"<[^>]+>", "", txt))[:300]
            elif ctag in ("guid", "id"):  # RSS <guid> / Atom <id> — the stable identity, when the feed has one
                guid = txt
        if title and link:
            items.append({
                "source": source,
                "title": html.unescape(title)[:200],
                "link": link,
                "summary": summary,
                "guid": guid,
            })
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

    try:
        ttl = int(os.environ.get("ZUGAMIND_NEWS_CACHE_TTL", str(_DEFAULT_CACHE_TTL)))
    except (TypeError, ValueError):
        ttl = _DEFAULT_CACHE_TTL  # malformed env var must not take the whole scanner down
    keywords = [k.strip().lower() for k in
                os.environ.get("ZUGAMIND_NEWS_KEYWORDS", "").split(",") if k.strip()]

    fetch_cache = _load_json(_FETCH_CACHE_FILE, {"ts": 0, "items": []})
    if not isinstance(fetch_cache, dict) or not isinstance(fetch_cache.get("items"), list):
        fetch_cache = {"ts": 0, "items": []}  # unexpected on-disk shape -- never crash on it, just re-fetch
    fetched_now = False
    if time.time() - fetch_cache.get("ts", 0) > ttl:
        items: list[dict[str, str]] = []
        for url in feed_urls:
            raw = _fetch(url)
            if not raw:
                continue
            items.extend(_parse_feed(raw, url))
        fetch_cache = {"ts": time.time(), "items": items}
        _save_json(_FETCH_CACHE_FILE, fetch_cache)
        fetched_now = True

    cache_items = [it for it in fetch_cache.get("items", []) if isinstance(it, dict)]

    cold_start = not _SEEN_FILE.exists()
    seen = _load_seen(_SEEN_FILE)
    now = time.time()

    if cold_start and cache_items:
        # First run ever (no seen-set on disk yet): baseline everything
        # currently in the feeds as already-seen and fire nothing. Without
        # this, pointing the scanner at feeds with months of back-catalogue
        # would dump a burst of old "news" into the workspace on first scan.
        # Same cold-start policy as scanners/world/ai_labs.py.
        for it in cache_items:
            link = it.get("link", "")
            if link:
                seen[it.get("guid") or link] = now
        _save_seen(_SEEN_FILE, seen, now)
        return []

    triggers: list[dict[str, Any]] = []
    newly_seen: set[str] = set()

    for it in cache_items:
        link = it.get("link", "")
        if not link:
            continue
        item_id = it.get("guid") or link
        # Checked against both the preferred id and the bare link so an
        # upgrade in place never re-fires something this scanner already
        # stamped under its link before guid/id extraction existed.
        is_new = item_id not in seen and link not in seen
        seen[item_id] = now  # refresh-on-observe: still in the feed -> still seen
        if not is_new:
            continue
        newly_seen.add(item_id)
        text = f"{it.get('title', '')} {it.get('summary', '')}".lower()
        if keywords and not any(kw in text for kw in keywords):
            continue
        detail = it.get("title", "")
        if it.get("summary"):
            detail += " -- " + it["summary"][:160]
        triggers.append({
            "type": "news_rss",
            "detail": detail[:280],
            "source": it.get("source", ""),
            "title": it.get("title", ""),
            "link": link,
            "novelty": 0.8,
            "relevance": 0.6,
            "urgency": 0.3,
        })
        if len(triggers) >= _MAX_TRIGGERS:
            break

    # Persist when something is new OR a fresh fetch re-stamped the window —
    # refresh-on-observe only works if the refreshed stamps reach disk at
    # least once per fetch. Gated on `seen` being non-empty too, so a fetch
    # cycle where every feed failed doesn't create an empty seen-set file
    # that would wrongly look like an already-baselined (non-cold-start) state
    # next call.
    if newly_seen or (fetched_now and seen):
        _save_seen(_SEEN_FILE, seen, now)

    return triggers
