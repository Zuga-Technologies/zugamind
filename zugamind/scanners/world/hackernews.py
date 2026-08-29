"""HackerNews scanner — top stories, filtered for AI/ML/business relevance.

Free public API (https://hacker-news.firebaseio.com/v0). No auth, no cost.
Emits one trigger per fresh top-30 story whose title hits the keyword filter.

Dedupe (audit 2026-08-29): habituation alone is a FORGETTING window, not a
memory — a top-30 story routinely outlives HABITUATION_HOURS(6h), so without
its own memory this scanner re-fired the same front-page story every window,
forever. Fixed the same way reddit_ai and ai_labs already were: a persistent
per-story seen-set (scanners.seen_items), keyed on str(story_id), baselined
silently on cold start so deploying this never reads the current front page
as a month of breaking news.

Emit cap: _MAX_TRIGGERS bounds one cycle to the best candidates (by urgency,
sorted before the cut) rather than every one of _MAX_STORIES — nothing
downstream was bounding this, and 30 triggers in one cycle dwarfs every
sibling world scanner's cap.

Domain: classifier will route most hits as CIVILIZATION (AI/ML topics) or
BUSINESS (startup/funding/launch news).

Caching: this was the worst offender —
1 topstories fetch + 30 per-item fetches = ~31 uncached HTTP round-trips every
~5-minute cycle. Now disk-cached at data/scanner_cache/hackernews.json:
  * the topstories list is cached for _TOP_TTL (≈ the scanner's cadence);
  * each story item is cached for _ITEM_TTL (items are effectively immutable);
  * story_ids already in the fresh item cache are NOT re-fetched.
Steady state drops to ~1 call/cycle (a topstories refresh) plus a fetch only for
genuinely new entrants. Both endpoints now go through scanners.safe_http's
fetch_json, so on top of that they get conditional GET (an unchanged
topstories list answers 304, no body) and 429 backoff (a throttled HN used to
read exactly like a dead feed). Stdlib-only, fail-silent — a cache miss/
corruption just falls back to a live fetch.
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import json as _json

from foundation.text_format import truncate_title
from scanners import safe_http
from scanners.seen_items import atomic_write_text, read_seen, write_seen

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
def _top_ttl() -> float:
    """Read at CALL time, tolerantly. This was a bare float() at module level,
    and hackernews is a STATIC import in scanners/__init__.py — so a typo'd
    ZUGAMIND_HN_TOP_TTL raised ValueError during import and stopped the whole
    runner from booting. The dynamically-imported scanners degrade to a
    warning for the same mistake; this one took the process down."""
    raw = (os.environ.get("ZUGAMIND_HN_TOP_TTL") or "").strip()
    if not raw:
        return 300.0
    try:
        return float(raw)
    except ValueError:
        logger.warning("hackernews: ZUGAMIND_HN_TOP_TTL=%r is not a number — "
                       "using 300", raw)
        return 300.0
_ITEM_TTL = float(os.environ.get("ZUGAMIND_HN_ITEM_TTL", "3600"))

# Keep stories matching any of these patterns. Tuned for AI/ML/founder content.
#
# The whole alternation sits inside \b(...)\b, so every alternative must end
# on a word character. Two did not, and matched NOTHING (audit 2026-08-29):
#   "fine-tun"      -> the \b after it demands a boundary right after "tun",
#                      so "fine-tuning" / "fine-tune" / "fine-tuned" all miss
#   "open[- ]?source" -> missed "open-sourcing" / "open-sourced"
# Both are now written with their own suffix alternation instead of relying
# on the shared trailing boundary. A dead branch in a KEEP filter is silent:
# it does not error, it just stops surfacing real stories.
_KEEP_RE = re.compile(
    r"\b(AI|ML|LLM|GPT|Claude|Anthropic|OpenAI|DeepMind|HuggingFace|"
    r"transformer|agent|agentic|RAG|fine[- ]?tun(?:e|ed|es|ing)|inference|"
    r"startup|funding|launch|YC|Y Combinator|Series [A-D]|seed round|"
    r"founder|acquisition|IPO|"
    r"Python|TypeScript|Rust|model|embedding|"
    r"alignment|safety|interpretability|hallucination|"
    r"benchmark|paper|arxiv|reasoning|"
    r"Stripe|Cloudflare|AWS|"
    r"open[- ]?sourc(?:e|ed|es|ing)|MIT|Apache|BSD)\b",
    re.IGNORECASE,
)
_MAX_STORIES = 30
# One cycle must not be able to put more into the workspace than every
# sibling world scanner combined (ai_labs=4, github_issues=5) — nothing
# downstream bounded this, so _MAX_STORIES itself (30) was the de-facto cap.
# Sort by urgency BEFORE slicing (see scan_hackernews) so the cap keeps the
# best candidates, not just whichever 4 happened to sit first in HN's own
# top-30 ranking.
_MAX_TRIGGERS = 4
# 30 candidates a sweep, and a story's trip through the top-30 is measured in
# hours-to-a-few-days, not weeks — new entrants turn over far slower than the
# per-sweep count, so this holds many weeks of history (same sizing logic as
# reddit_ai's and ai_labs's own seen-sets).
_SEEN_MAX = 900

# Brand watch: mentions of YOUR project outrank ambient industry news.
# ZUGAMIND_BRAND_TERMS is a comma-separated term list (e.g. "zugamind,zuga");
# unset = feature off. A title hit bypasses the topical keyword filter — a
# brand mention is wake-worthy even when it shares no words with the AI/ML
# vocabulary — and emits at high salience (though below the alarm lane:
# someone discussing your project is urgent-ish, not an outage).
_BRAND_TERMS = [t.strip() for t in
                os.environ.get("ZUGAMIND_BRAND_TERMS", "").split(",") if t.strip()]
_BRAND_RE = (re.compile("|".join(re.escape(t) for t in _BRAND_TERMS), re.IGNORECASE)
             if _BRAND_TERMS else None)

# `detail` is the literal briefing text handed to a paid model, and a title
# is third-party, untrusted text — a newline or control byte inside it is a
# prompt-injection seam, not cosmetic noise. Collapse to single spaces FIRST,
# then hand the result to truncate_title, so a title padded with control
# characters can't dodge the length cut by hiding real content past it.
_CONTROL_WS_RE = re.compile(r"[\s\x00-\x1f\x7f]+")


def _clean_title(title: str) -> str:
    return truncate_title(_CONTROL_WS_RE.sub(" ", title or "").strip(), 140)


# Per-URL safe_http state (etag/last_modified/blocked_until/fails), kept for
# the life of the process — item URLs are one-per-story and each fetched at
# most a handful of times ever, so there is no on-disk cache format to grow
# for this; it just needs to survive across cycles within one running daemon
# so a 429 backoff and an ETag actually get reused. See safe_http's own
# module docstring for the "four scanners, four re-implementations" history
# this replaces.
_HTTP_STATE: dict[str, dict] = {}


def _fetch_json(url: str) -> Any:
    """One HTTP GET, routed through safe_http. This used to be a bare
    urlopen() with no conditional GET and no 429 handling, so a throttled HN
    read exactly like a dead feed instead of a rate limit."""
    state = _HTTP_STATE.setdefault(url, {})
    status, data = safe_http.fetch_json(url, state=state, timeout=_TIMEOUT,
                                        name=f"hackernews:{url}")
    if status == "ok":
        state["_last_ok"] = data
        return data
    if status == "not_modified":
        return state.get("_last_ok")
    logger.debug("hn fetch failed for %s (status=%s)", url, status)
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
        atomic_write_text(_CACHE_PATH, _json.dumps(cache))
    except Exception as e:  # persistence best-effort — never break the cycle
        logger.debug("hn cache save failed (non-fatal): %s", e)


def _seen_path() -> Path:
    # Derived from _CACHE_PATH (not a fixed module constant) so a test that
    # isolates the disk cache by patching _CACHE_PATH isolates the seen-set
    # right along with it — the same reasoning ai_labs uses for _seen_file().
    return _CACHE_PATH.parent / "hackernews_seen.json"


def _top_ids(cache: dict, now: float) -> list:
    """Top-story ids, from cache when fresh, else one live fetch."""
    top = cache.get("top") or {}
    if isinstance(top, dict) and (now - safe_http.num(top.get("ts"))) < _top_ttl():
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
    top_slice = top[:_MAX_STORIES]

    seen = read_seen(_seen_path())
    # None means "no seen-set on disk at all" — a genuine cold start, not an
    # empty memory. Baseline it silently below rather than reading the
    # current front page as a month of breaking news the instant this ships.
    cold_start = seen is None
    if cold_start:
        seen = {}

    candidates: list[dict] = []
    for sid in top_slice:
        item = _item(cache, sid, now)
        if not item or not isinstance(item, dict):
            continue
        title = item.get("title", "") or ""
        if not title:
            continue
        brand_hit = bool(_BRAND_RE and _BRAND_RE.search(title))
        if not brand_hit and not _KEEP_RE.search(title):
            continue
        key = str(sid)
        if not cold_start and key in seen:
            # Already emitted once — a top-30 story routinely outlives the 6h
            # habituation window and would otherwise re-fire every window,
            # forever (the whole reason this seen-set exists).
            continue
        url = item.get("url", "") or ""
        # Third-party numeric — a null/string/negative score must not raise
        # mid-sweep or silently win every comparison as NaN.
        score = safe_http.num(item.get("score"), 0.0)
        clean_title = _clean_title(title)
        # Engagement velocity (points/hour) is free in the API — a story at
        # 100pts/hour deserves to out-bid one that took a day to get there.
        # 0.3 floor = the old flat prior; cap 0.65 keeps ambient news from
        # ever reaching the alarm lane on velocity alone.
        age_h = max((now - safe_http.num(item.get("time"), now)) / 3600.0, 0.5)
        # clamp01 both ends. The old min()-only clamp let a negative or
        # non-numeric score out of the 0..1 contract entirely —
        # measured -2499.7 from a negative score, and NaN read as the
        # maximum. urgency is auction currency: an out-of-range value
        # does not look wrong, it outbids every honest sense.
        urgency = round(safe_http.clamp01(
            min(0.65, 0.3 + (score / age_h) / 400.0)), 3)
        trig = {
            "type": "hackernews_story",
            "detail": f"HN [{int(score)}pts]: {clean_title}",
            "url": url,
            "story_id": sid,
            "score": score,
            "novelty": safe_http.clamp01(0.55),
            "relevance": safe_http.clamp01(0.5),
            "urgency": urgency,
        }
        if brand_hit:
            trig["detail"] = f"HN BRAND MENTION [{int(score)}pts]: {clean_title}"
            trig["brand_mention"] = True
            trig["novelty"] = safe_http.clamp01(0.9)
            trig["relevance"] = safe_http.clamp01(0.9)
            trig["urgency"] = safe_http.clamp01(max(0.75, urgency))
        candidates.append(trig)

    if cold_start:
        # Whole current front page marked seen, zero triggers — same silent
        # baseline reddit_ai and ai_labs use. Cost: a story published inside
        # this very sweep is missed once.
        write_seen(_seen_path(), {str(sid): now for sid in top_slice}, _SEEN_MAX)
        out: list[dict] = []
    else:
        # Urgency descending BEFORE the cap: HN's own top-30 order is a
        # ranking, not a priority order for US, so _MAX_TRIGGERS must keep
        # the best candidates rather than whichever ones sat first in it.
        candidates.sort(key=lambda t: t["urgency"], reverse=True)
        out = candidates[:_MAX_TRIGGERS]
        for t in out:
            seen[str(t["story_id"])] = now
        if out:
            # protect = every story id still on the front page this sweep —
            # see seen_items.write_seen's docstring. The stored stamp is
            # FIRST-seen and never refreshed, so a story that sits on the
            # front page for days holds the OLDEST stamp in the set and would
            # be the first thing evicted at the cap, then re-fire as new.
            write_seen(_seen_path(), seen, _SEEN_MAX,
                       protect={str(sid) for sid in top_slice})

    _prune_items(cache, now)
    _save_cache(cache)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for t in scan_hackernews()[:5]:
        print(t["detail"][:100])
