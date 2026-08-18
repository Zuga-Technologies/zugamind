"""AI lab research scanner — polls canonical AI lab feeds.

Direct source for cutting-edge research: Anthropic, OpenAI, DeepMind,
Google Research, Hugging Face Papers, Microsoft Research. Emits one trigger
per post it has never emitted before. Surfaces as CIVILIZATION-domain
research input for the self-modification bridge.

Two things here are load-bearing and were both missing until 2026-08-18,
when a Jul-24 post ("Introducing Claude Opus 5") won the global workspace
eight separate times over twelve days and bought a harness wake:

1. DEDUPE IS PER-ITEM AND PERSISTENT (`ai_labs_seen.json`), the same "seen
   link" shape the sibling example scanners use. The 30-minute `_CACHE_TTL`
   is a FETCH cache, not a dedupe — it only limits how often we hit the
   network. Engine-level habituation (`HABITUATION_HOURS`, default 6) is a
   rate limiter, not a dedupe either: without a seen-set, a post that stays
   at the top of a feed re-fires every 6 hours forever. On a cold start the
   seen-set is baselined SILENTLY (whole feed marked seen, zero triggers) so
   deploying this never dumps a month of back-catalogue into the workspace.

2. URGENCY DECAYS WITH PUBLISH AGE. Feed order is not recency order —
   anthropic.com/news leads with a pinned/featured block, so its FIRST card
   was a month old while the newest post sat fifth and was never reached.
   A trigger's numbers have to be evidence: a constant urgency means the bid
   is identical whether the item is an hour old or a year old. Age unknown
   (unparseable date) fails OPEN at the fresh value — a parser regression
   must not silently make the scanner deaf.

Stdlib only. Failure-silent per scanner contract. Cached 30min on disk.
"""
from __future__ import annotations

import calendar
import email.utils
import json
import logging
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from foundation.text_format import truncate_title

logger = logging.getLogger("zugamind.scanners.ai_labs")

_TIMEOUT = 8.0
_CACHE_TTL = 1800
_MAX_TRIGGERS = 4
_SEEN_MAX = 800     # seven feeds x eight items = 56 keys/sweep; this holds ~2 weeks
_FRESH_HOURS = 24   # published inside this window scores full urgency
_STALE_HOURS = 72   # published beyond this scores zero — backlog, not news
# Honors ZUGAMIND_DATA_DIR without importing foundation — scanners stay standalone.
_DATA_DIR = Path(os.environ.get("ZUGAMIND_DATA_DIR") or Path(__file__).resolve().parent.parent.parent / "data")
_CACHE_DIR = _DATA_DIR / "scanner_cache"


# Resolved per call, not bound at import: tests isolate this scanner by
# patching _CACHE_DIR alone (tests/conftest.py), and a module-level
# `_CACHE_FILE = _CACHE_DIR / ...` would keep pointing at the real data
# directory after that patch — which is exactly how the pre-2026-08-18
# constant let the suite read and overwrite the live cache.
def _cache_file() -> Path:
    return _CACHE_DIR / "ai_labs.json"


def _seen_file() -> Path:
    return _CACHE_DIR / "ai_labs_seen.json"

# Feed audit: anthropic never had an RSS feed (404 since introduction; the
# /news and /research indexes ARE server-rendered, so scrape them), deepmind
# moved off /discover, meta_fair has no feed at all (removed), hf_papers RSS
# is auth-walled but the JSON API is open. Third field picks the parser.
_FEEDS = [
    ("anthropic",      "https://www.anthropic.com/news",                   "anthropic_html"),
    ("anthropic_res",  "https://www.anthropic.com/research",               "anthropic_html"),
    ("openai",         "https://openai.com/news/rss.xml",                  "rss"),
    ("deepmind",       "https://deepmind.google/blog/rss.xml",             "rss"),
    ("google_res",     "https://research.google/blog/rss/",                "rss"),
    ("hf_papers",      "https://huggingface.co/api/daily_papers?limit=10", "hf_json"),
    ("msft_research",  "https://www.microsoft.com/en-us/research/feed/",   "rss"),
]

_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _read_cache() -> dict[str, Any] | None:
    try:
        path = _cache_file()
        if not path.exists():
            return None
        d = json.loads(path.read_text())
        if time.time() - d.get("ts", 0) > _CACHE_TTL:
            return None
        return d
    except Exception:
        return None


def _write_cache(payload: dict[str, Any]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_file().write_text(json.dumps(payload))
    except Exception:
        pass


def _read_seen() -> dict[str, float] | None:
    """Item key -> epoch first emitted. None means "no seen-set on disk yet",
    which is a COLD START and must be distinguished from an empty one — the
    first is baselined silently, the second really has nothing seen."""
    try:
        path = _seen_file()
        if not path.exists():
            return None
        d = json.loads(path.read_text())
        return {str(k): float(v) for k, v in d.items()} if isinstance(d, dict) else None
    except Exception:
        return None


def _write_seen(seen: dict[str, float]) -> None:
    """Persist newest _SEEN_MAX keys. Pruning is by recency, never by key
    order: an alphabetical cap would evict a live feed item and let it fire
    again the next sweep."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if len(seen) > _SEEN_MAX:
            keep = sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:_SEEN_MAX]
            seen = dict(keep)
        _seen_file().write_text(json.dumps(seen))
    except Exception:
        pass


def _item_key(it: dict[str, Any]) -> str:
    """Stable identity for one feed item. The permalink where a feed gives
    one; lab+title otherwise (hf_papers without an id, a malformed entry)."""
    link = (it.get("link") or "").strip()
    return link or f"{it.get('lab', '?')}|{it.get('title', '')}"


_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
# "Product Jul 24, 2026 Introducing ..." / "Aug 14, 2026 Announcements ..." —
# anthropic.com renders the date inside the card, in either position.
_TEXT_DATE_RE = re.compile(r"\b([A-Z][a-z]{2})[a-z]*\s+(\d{1,2}),\s+(\d{4})\b")


def _parse_date(value: str) -> float | None:
    """Epoch seconds from an RFC-822 (RSS), ISO-8601 (Atom/JSON), or
    "Mon D, YYYY" (scraped card) date. None when nothing parses."""
    value = (value or "").strip()
    if not value:
        return None
    try:  # RFC 822 — "Tue, 14 Aug 2026 09:00:00 +0000"
        dt = email.utils.parsedate_to_datetime(value)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
    except Exception:
        pass
    try:  # ISO 8601 — "2026-08-14T09:00:00Z"
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        pass
    m = _TEXT_DATE_RE.search(value)
    if m:
        month = _MONTHS.get(m.group(1).lower())
        if month:
            try:
                return datetime(int(m.group(3)), month, int(m.group(2)),
                                tzinfo=timezone.utc).timestamp()
            except ValueError:
                return None
    return None


def _urgency_for(published: float | None, now: float) -> float:
    """Urgency is publish age, not a constant. Unknown age fails OPEN at the
    fresh value: a feed that stops exposing dates must not go silent."""
    if published is None:
        return 0.25
    age_h = max(0.0, (now - published) / 3600.0)
    if age_h <= _FRESH_HOURS:
        return 0.25
    if age_h >= _STALE_HOURS:
        return 0.0
    span = _STALE_HOURS - _FRESH_HOURS
    return round(0.25 * (1.0 - (age_h - _FRESH_HOURS) / span), 4)


def _fetch(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ZugaMind/scanner"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        logger.debug("ai_labs fetch %s failed: %s", url, e)
        return None


def _parse_feed(xml_text: str, lab: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return items
    for it in root.iter():
        tag = it.tag.lower().split("}")[-1]
        if tag in ("item", "entry"):
            title = ""
            link = ""
            summary = ""
            published = None
            for child in it:
                ctag = child.tag.lower().split("}")[-1]
                txt = (child.text or "").strip()
                if ctag == "title":
                    title = txt
                elif ctag == "link":
                    href = child.attrib.get("href")
                    link = href if href else txt
                elif ctag in ("description", "summary"):
                    summary = truncate_title(re.sub(r"<[^>]+>", "", txt), 300)
                elif ctag in ("pubdate", "published", "updated", "date") and published is None:
                    published = _parse_date(txt)
            if title:
                items.append({"lab": lab, "title": truncate_title(title, 200), "link": link,
                              "summary": summary, "published": published})
        if len(items) >= 8:
            break
    return items


def _parse_anthropic_html(html: str, lab: str) -> list[dict[str, str]]:
    """Scrape a server-rendered anthropic.com index page (no RSS exists).

    Covers both /news/<slug> and /research/<slug> cards — research posts
    do not appear on /news.
    """
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for m in re.finditer(r'<a[^>]+href="(/(?:news|research)/[a-z0-9-]+)"[^>]*>(.*?)</a>', html, re.S):
        href, inner = m.group(1), m.group(2)
        if href in seen:
            continue
        seen.add(href)
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", inner)).strip()
        if len(title) < 8:  # skip bare nav anchors
            continue
        items.append({"lab": lab, "title": title[:200],
                      "link": "https://www.anthropic.com" + href, "summary": "",
                      "published": _parse_date(title)})
        if len(items) >= 8:
            break
    return items


def _parse_hf_json(txt: str) -> list[dict[str, str]]:
    """Parse the open huggingface.co/api/daily_papers JSON (RSS is auth-walled)."""
    items: list[dict[str, str]] = []
    try:
        data = json.loads(txt)
    except Exception:
        return items
    for p in data[:8] if isinstance(data, list) else []:
        if not isinstance(p, dict):
            continue
        paper = p.get("paper") if isinstance(p.get("paper"), dict) else p
        title = (paper.get("title") or "").strip().replace("\n", " ")
        if not title:
            continue
        pid = paper.get("id") or p.get("id") or ""
        published = _parse_date(str(paper.get("publishedAt") or p.get("publishedAt") or ""))
        items.append({"lab": "hf_papers", "title": title[:200],
                      "link": f"https://huggingface.co/papers/{pid}" if pid else "",
                      "summary": (paper.get("summary") or "")[:300],
                      "published": published})
    return items


def _round_robin(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """Interleave items per lab so one feed can't monopolize _MAX_TRIGGERS."""
    by_lab: dict[str, list] = {}
    for it in items:
        by_lab.setdefault(it.get("lab", "?"), []).append(it)
    ordered: list[dict[str, str]] = []
    while any(by_lab.values()):
        for lab_items in by_lab.values():
            if lab_items:
                ordered.append(lab_items.pop(0))
    return ordered


def scan_ai_labs() -> list[dict[str, Any]]:
    cached = _read_cache()
    if cached and "items" in cached:
        items = cached["items"]
    else:
        items = []
        for lab, url, fmt in _FEEDS:
            txt = _fetch(url)
            if not txt:
                continue
            if fmt == "anthropic_html":
                items.extend(_parse_anthropic_html(txt, lab))
            elif fmt == "hf_json":
                items.extend(_parse_hf_json(txt))
            else:
                items.extend(_parse_feed(txt, lab))
        _write_cache({"ts": time.time(), "items": items})

    if not items:
        return []  # every feed down: stay cold rather than baseline an empty sweep

    seen = _read_seen()
    if seen is None:
        # Cold start: baseline the whole sweep and say nothing. Without this,
        # deploying the seen-set would read a month of back-catalogue as
        # breaking news and burst _MAX_TRIGGERS of it into the workspace.
        # Cost: a post published inside this very sweep is missed once.
        _write_seen({_item_key(it): time.time() for it in items})
        return []

    now = time.time()
    unseen = [it for it in items if _item_key(it) not in seen]
    # Newest first WITHIN each lab before round-robin: feed order is publisher
    # order, and a pinned/featured block puts month-old posts at the top.
    unseen.sort(key=lambda it: it.get("published") or 0.0, reverse=True)

    triggers: list[dict[str, Any]] = []
    for it in _round_robin(unseen)[:_MAX_TRIGGERS]:
        detail = f"[{it['lab']}] {it['title']}"
        if it.get("summary"):
            detail += " -- " + it["summary"][:160]
        published = it.get("published")
        seen[_item_key(it)] = now
        triggers.append({
            "type": "ai_lab_research",
            "detail": detail[:380],
            "lab": it["lab"],
            "title": it["title"],
            "link": it.get("link", ""),
            "summary": it.get("summary", ""),
            "published": published,
            "novelty": 0.8,
            "relevance": 0.75,
            "urgency": _urgency_for(published, now),
        })

    if triggers:
        _write_seen(seen)
    return triggers
