"""HackerNews scanner — top stories, filtered for AI/ML/business relevance.

Free public API (https://hacker-news.firebaseio.com/v0). No auth, no cost.
Emits one trigger per fresh top-30 story whose title hits the keyword filter.
Habituation dedupes by story_id within HABITUATION_HOURS.

Domain: classifier will route most hits as CIVILIZATION (AI/ML topics) or
BUSINESS (startup/funding/launch news).

Caching: this was the worst offender —
1 topstories fetch + 30 per-item fetches = ~31 uncached HTTP round-trips every
~5-minute cycle. Now disk-cached at data/scanner_cache/hackernews.json:
  * the topstories list is cached for _TOP_TTL (≈ the scanner's cadence);
  * each story item is cached for _ITEM_TTL (items are effectively immutable);
  * story_ids already in the fresh item cache are NOT re-fetched.
Steady state drops to ~1 call/cycle (a topstories refresh) plus a fetch only for
genuinely new entrants. Stdlib-only, fail-silent — a cache miss/corruption just
falls back to a live fetch.
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import urllib.request
import json as _json

logger = logging.getLogger("zugamind.scanners.hackernews")

_TOP_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
_TIMEOUT = 6.0

# Cache file + TTLs. TTLs are env-overridable so cadence tuning needs no
# code change. _TOP_TTL ≈ the HN cadence; items are immutable so cache them long.
# Honors ZUGAMIND_DATA_DIR (the same override foundation.config uses) without
# importing foundation — scanners stay standalone.
_DATA_DIR = Path(os.environ.get("ZUGAMIND_DATA_DIR") or Path(__file__).resolve().parent.parent.parent / "data")
_CACHE_PATH = _DATA_DIR / "scanner_cache" / "hackernews.json"
_TOP_TTL = float(os.environ.get("ZUGAMIND_HN_TOP_TTL", "300"))
_ITEM_TTL = float(os.environ.get("ZUGAMIND_HN_ITEM_TTL", "3600"))

# Keep stories matching any of these patterns. Tuned for AI/ML/founder content.
_KEEP_RE = re.compile(
    r"\b(AI|ML|LLM|GPT|Claude|Anthropic|OpenAI|DeepMind|HuggingFace|"
    r"transformer|agent|agentic|RAG|fine-tun|inference|"
    r"startup|funding|launch|YC|Y Combinator|Series [A-D]|seed round|"
    r"founder|acquisition|IPO|"
    r"Python|TypeScript|Rust|model|embedding|"
    r"alignment|safety|interpretability|hallucination|"
    r"benchmark|paper|arxiv|reasoning|"
    r"Stripe|Cloudflare|AWS|"
    r"open[- ]?source|MIT|Apache|BSD)\b",
    re.IGNORECASE,
)
_MAX_STORIES = 30


def _fetch_json(url: str) -> Any:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ZugaMind/scanner"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return _json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.debug("hn fetch failed for %s: %s", url, e)
        return None


def _load_cache() -> dict:
    try:
        if _CACHE_PATH.exists():
            data = _json.loads(_CACHE_PATH.read_text("utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as e:  # corrupt cache is non-fatal — fall back to live fetch
        logger.debug("hn cache load failed (ignoring): %s", e)
    return {}


def _save_cache(cache: dict) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps(cache), "utf-8")
        tmp.replace(_CACHE_PATH)
    except Exception as e:  # persistence best-effort — never break the cycle
        logger.debug("hn cache save failed (non-fatal): %s", e)


def _top_ids(cache: dict, now: float) -> list:
    """Top-story ids, from cache when fresh, else one live fetch."""
    top = cache.get("top") or {}
    if isinstance(top, dict) and (now - float(top.get("ts", 0))) < _TOP_TTL:
        ids = top.get("ids")
        if isinstance(ids, list):
            return ids
    fetched = _fetch_json(_TOP_URL)
    if isinstance(fetched, list):
        cache["top"] = {"ts": now, "ids": fetched}
        return fetched
    # Fetch failed: reuse stale ids if we have any, else give up this cycle.
    ids = top.get("ids") if isinstance(top, dict) else None
    return ids if isinstance(ids, list) else []


def _item(cache: dict, sid: Any, now: float) -> Any:
    """One story item, from cache when fresh, else one live fetch. Dedups by id."""
    items = cache.setdefault("items", {})
    key = str(sid)
    hit = items.get(key)
    if isinstance(hit, dict) and (now - float(hit.get("ts", 0))) < _ITEM_TTL:
        return hit.get("data")
    data = _fetch_json(_ITEM_URL.format(id=sid))
    if isinstance(data, dict):
        items[key] = {"ts": now, "data": data}
    return data


def _prune_items(cache: dict, now: float) -> None:
    """Drop item entries past TTL so the cache file stays bounded."""
    items = cache.get("items")
    if not isinstance(items, dict):
        return
    stale = [k for k, v in items.items()
             if not isinstance(v, dict) or (now - float(v.get("ts", 0))) >= _ITEM_TTL]
    for k in stale:
        items.pop(k, None)


def scan_hackernews() -> list[dict]:
    now = time.time()
    cache = _load_cache()
    top = _top_ids(cache, now)
    if not top:
        return []
    out: list[dict] = []
    for sid in top[:_MAX_STORIES]:
        item = _item(cache, sid, now)
        if not item or not isinstance(item, dict):
            continue
        title = item.get("title", "") or ""
        url = item.get("url", "") or ""
        score = item.get("score", 0) or 0
        if not title:
            continue
        if not _KEEP_RE.search(title):
            continue
        out.append({
            "type": "hackernews_story",
            "detail": f"HN [{score}pts]: {title[:140]}",
            "url": url,
            "story_id": sid,
            "score": score,
            "novelty": 0.55,
            "relevance": 0.5,
            "urgency": 0.3,
        })
    _prune_items(cache, now)
    _save_cache(cache)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for t in scan_hackernews()[:5]:
        print(t["detail"][:100])
