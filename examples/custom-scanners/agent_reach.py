"""Example private scanner — Agent-Reach web-watch + search adapter.

Not part of the ZugaMind package; a worked example of the extra_scanners
pattern documented in examples/custom-scanners/README.md. Copy this file
into your own deployment and pass it to
StreamRunner(extra_scanners={"scan_agent_reach": scan_agent_reach}).

Agent-Reach (github.com/Panniantong/Agent-Reach) is a router, not a single
API — it fronts several channels, each an upstream tool with its own output
shape. This adapter wires in the two channels that need no cookies, no
login session, and no ban risk, and normalizes both into the standard
trigger contract (type/detail/novelty/relevance/urgency, <=5 per cycle):

  1. web_watch — any URL you configure, fetched as plain text/markdown via
     Jina Reader (a plain HTTPS GET to https://r.jina.ai/<url>, no API key).
     Change is detected by content hash: the first time a URL is seen its
     hash is recorded as a silent baseline (no trigger — there's nothing to
     compare against yet); every fetch after that triggers `reach_web_update`
     only if the hash actually moved. A moved hash is then DIFFED against the
     previous line-set, and the trigger carries the lines that were ADDED —
     not the top of the page — with relevance scored on those added lines
     alone. A change that adds nothing (reorder, removal, rotating promo)
     advances the baseline silently instead of reporting itself as news.
  2. search — standing web-search queries, backend chain in order:
       a. Exa via `mcporter call exa.web_search_exa` (Agent-Reach's native
          route) when `mcporter` is on PATH;
       b. Tavily REST (https://api.tavily.com/search) when TAVILY_API_KEY
          is set — plain stdlib HTTPS POST, no extra install;
       c. neither available -> channel silently off, not an error.
     Search runs on its OWN slower cadence (ZUGAMIND_REACH_SEARCH_TTL,
     default 6h) because every query poll is real upstream API spend,
     unlike the watch channel's free Jina fetches.

Injected scanners bypass ZugaMind's habituation filter (see the runner's
extra_scanners contract in the README), so this adapter keeps its own
on-disk fetch cache + seen-set, same pattern as x_activity.py / news_rss.py
in this directory.

Honesty note on "real time": both channels are poll-based, gated by
ZUGAMIND_REACH_CACHE_TTL. A page watch only re-checks a URL every TTL
seconds, and a search query only re-runs every TTL seconds — this is meant
to catch a real change or a new top result within roughly an hour by
default, not to watch anything sub-minute.

Configuration (env):
    ZUGAMIND_REACH_WATCH_URLS   comma-separated URLs to watch for content
                                change. Optional.
    ZUGAMIND_REACH_QUERIES      comma-separated Exa search queries.
                                Optional.
    (at least one of the above two must be set — unset means the whole
    scanner is off, returns [].)
    ZUGAMIND_REACH_CACHE_TTL    seconds between watch-channel re-fetches.
                                Default 3600 (60 min) — not tuned for
                                sub-minute freshness by design.
    ZUGAMIND_REACH_SEARCH_TTL   seconds between search-channel re-polls.
                                Default 21600 (6 h) — each poll is real
                                upstream API spend (Exa or Tavily), and a
                                standing query's answer set doesn't churn
                                by the hour.
    TAVILY_API_KEY              enables the Tavily search backend when
                                mcporter is absent. Optional.
    ZUGAMIND_REACH_KEYWORDS     optional comma-separated keywords, case-
                                insensitive. If set, relevance scores higher
                                for hits and lower for misses; if unset,
                                every trigger gets a neutral 0.5 relevance.
                                Search results are scored on the keywords
                                the query did NOT already contain — see
                                _keyword_relevance.
    ZUGAMIND_REACH_SEARCH_TIME_RANGE
                                Tavily backend only: the publish/update
                                window results are restricted to —
                                day | week | month | year. Default "week".
                                Set to "" for no window. A standing query
                                is asking "what is NEW", and Tavily returns
                                no publish dates, so a result's age cannot
                                be corrected after the fact — the request
                                window IS the freshness evidence. Exa does
                                report `publishedDate`, and that is scored
                                as urgency (see _search_urgency).

Search scoring, and why it is not the constants it used to be: a search
result's relevance was the keyword count over title+snippet, and its
urgency a flat 0.2. Both were constants for the channel — every top-5 hit
for "open source AI agent ..." contains "open source" and "agent" by
construction, and a flat urgency prices a post from four months ago as
today's. Each bought a real session (2026-08-22, an evergreen repo newly
in the top-5; 2026-08-28, a dev.to post from 2026-04-25 whose repo had
been dormant since June). A trigger's numbers have to be able to vary per
fire, or the scanner is asserting, not scoring.

Dedupe: web_watch keys off a per-URL content hash plus the per-URL line-set
that hash summarizes, both persisted to disk; search
keys off "seen (query, result-url) pair" persisted to disk, same "seen set
capped at 1000" pattern as the other examples in this directory. If more
candidates surface in one cycle than the 5-trigger cap allows, only the
emitted ones are marked seen/hash-advanced — the rest are left pending so
they get a fair shot at surfacing on a later cycle instead of being
silently and permanently dropped.

Stdlib only (urllib.request, subprocess, hashlib) — not a hard requirement
for *your own* private scanner (only core PRs are stdlib-constrained, see
CONTRIBUTING.md), but it means you can copy this and run it with zero
`pip install` beyond having `mcporter` itself on PATH for the search channel.

Fail-silent per scanner contract — one broken URL or a down Exa channel
does not sink the other channel or the other configured URLs/queries.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("zugamind.examples.agent_reach")

_TIMEOUT = 20.0  # Jina Reader renders + mcporter subprocess both run slower than a bare API call
_DEFAULT_CACHE_TTL = 3600
_DEFAULT_SEARCH_TTL = 21600
_MAX_TRIGGERS = 5
_SEEN_MAX = 1000
_WATCH_LINES_MAX = 600  # per-URL line-key budget; ~40KB of page holds well under this
_WATCH_LINKS_MAX = 400  # per-URL link budget; a news index carries tens, not hundreds
_DATA_DIR = Path(os.environ.get("ZUGAMIND_DATA_DIR") or Path(__file__).resolve().parent / "data")
_CACHE_DIR = _DATA_DIR / "scanner_cache"
_FETCH_CACHE_FILE = _CACHE_DIR / "agent_reach_fetch.json"
_SEEN_FILE = _CACHE_DIR / "agent_reach_seen.json"


def _load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("agent_reach cache load failed (%s): %s", path.name, e)
    return default


def _save_json(path: Path, payload: Any) -> None:
    """Atomic write (tmp + replace) — a fetch cache or seen-set torn mid-write
    by a killed process is worse than a stale one; the cadence gate above
    means a lost write just means one extra fetch/re-check next cycle."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        logger.debug("agent_reach cache save failed (%s): %s", path.name, e)


def _csv_env(name: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


def _keyword_relevance(text: str, exclude: str = "") -> float:
    """Deterministic relevance from the optional keyword list (no LLM,
    no network) — same shape as every other scanner in this directory.

    `exclude`: text whose own words must not count as evidence. The search
    channel passes the query: a result for "open source AI agent cognition
    attention" contains "open source" and "agent" BY CONSTRUCTION, so
    counting them pinned every top-5 hit at the 0.9 ceiling (0.25 + 0.4*0.9
    + 0.2*0.2 = 0.65 against a 0.570 floor) and bought two real sessions
    (2026-08-22, 2026-08-28). A keyword the query already contains is not
    something the result revealed; only the keywords it was NOT asked for
    can vary from one hit to the next. With every keyword excluded the
    answer is "no hits" (0.2), not the unconfigured neutral 0.5 — the list
    was set, and nothing outside the query matched."""
    keywords = _csv_env("ZUGAMIND_REACH_KEYWORDS")
    if not keywords:
        return 0.5
    if exclude:
        ex = exclude.lower()
        keywords = [k for k in keywords if k.lower() not in ex]
    hits = sum(1 for k in keywords if k.lower() in text.lower())
    return min(0.9, 0.3 + 0.2 * hits) if hits else 0.2


# ---------------------------------------------------------------- web_watch --

def _content_lines(body: str) -> list[str]:
    """Whitespace-normalized, non-empty lines — the unit a page diff is taken
    in. Normalizing here means a reflow or an indent change is not mistaken
    for new content."""
    return [line for line in (" ".join(l.split()) for l in body.splitlines()) if line]


def _line_key(line: str) -> str:
    """Short digest of one line — the cache stores keys, not the page, so a
    watched page costs ~12 bytes/line on disk instead of its full body."""
    return hashlib.sha1(line.encode("utf-8")).hexdigest()[:12]


_LINK_RE = re.compile(r"https?://[^\s)\]<>\"']+")


def _page_links(body: str) -> list[str]:
    """Every URL the page currently points at, in order, deduped."""
    out, seen = [], set()
    for url in _LINK_RE.findall(body):
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _added_lines(body: str, previous_keys: list[str]) -> list[str]:
    """Lines present now that were not present at the last baseline.

    Set membership, not a positional diff: a reordered page (a promo card
    rotating to the top, a "featured" slot reshuffling) yields ZERO added
    lines, which is the honest answer — nothing new was published."""
    seen = set(previous_keys)
    out, emitted = [], set()
    for line in _content_lines(body):
        key = _line_key(line)
        if key in seen or key in emitted:
            continue
        emitted.add(key)
        out.append(line)
    return out


def _fetch_jina(url: str) -> str | None:
    """Any page as markdown via Jina Reader — plain GET, no key, no deps."""
    try:
        req = urllib.request.Request(
            f"https://r.jina.ai/{url}",
            headers={"User-Agent": "ZugaMind/example-scanner"},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.read(200_000).decode("utf-8", errors="replace")
    except Exception as e:
        logger.debug("agent_reach: jina fetch %s failed: %s", url, e)
        return None


# ------------------------------------------------------------------- search --

_SEARCH_FRESH_HOURS = 24        # published inside this window scores full urgency
_SEARCH_STALE_HOURS = 72        # published beyond this scores zero — an old page, not news
_SEARCH_URGENCY_FRESH = 0.25
_SEARCH_URGENCY_UNDATED = 0.1   # no publish date at all: the backend could not vouch for freshness
_DEFAULT_SEARCH_TIME_RANGE = "week"


def _search_time_range() -> str:
    """Publish/update window asked of the Tavily backend ("" = no window)."""
    return os.environ.get("ZUGAMIND_REACH_SEARCH_TIME_RANGE", _DEFAULT_SEARCH_TIME_RANGE).strip()


def _parse_published(value: Any) -> float | None:
    """ISO-8601 publish stamp (Exa's `publishedDate`) -> epoch seconds, or
    None when the backend sent nothing parseable. A naive stamp is read as
    UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _search_urgency(published: float | None, now: float) -> float:
    """Urgency is publish age, not a constant. Same ramp as the package's
    ai_labs scanner (full inside 24h, linear to zero at 72h) — but an
    UNDATED result fails LOW, not open. ai_labs fails open because a
    curated feed that stops exposing dates must not go silent; a search
    engine's top-5 for a standing query is an evergreen answer set by
    nature, so "no date" there means "could be any age", and the flat 0.2
    this replaces let a 2026-04-25 post bid as if it were today's."""
    if published is None:
        return _SEARCH_URGENCY_UNDATED
    age_h = max(0.0, (now - published) / 3600.0)
    if age_h <= _SEARCH_FRESH_HOURS:
        return _SEARCH_URGENCY_FRESH
    if age_h >= _SEARCH_STALE_HOURS:
        return 0.0
    span = _SEARCH_STALE_HOURS - _SEARCH_FRESH_HOURS
    return round(_SEARCH_URGENCY_FRESH * (1.0 - (age_h - _SEARCH_FRESH_HOURS) / span), 4)


def _mcporter_available() -> bool:
    return shutil.which("mcporter") is not None


def _fetch_exa_results(query: str) -> list[dict[str, Any]]:
    """Run one Exa search through mcporter. Returns [] on any failure —
    missing binary, timeout, non-zero exit, or unparseable output."""
    try:
        proc = subprocess.run(
            ["mcporter", "call", "exa.web_search_exa",
             f"query={query}", "numResults=5"],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        data = json.loads(proc.stdout)
        results = data.get("results", []) if isinstance(data, dict) else []
        return results if isinstance(results, list) else []
    except Exception as e:
        logger.debug("agent_reach: exa search %r failed: %s", query, e)
        return []


def _fetch_tavily_results(query: str) -> list[dict[str, Any]]:
    """Run one Tavily search via plain REST (same request shape as the
    known-working Documentus connector). Normalizes Tavily's `content`
    field to `text` so the candidate loop treats both backends alike."""
    api_key = os.environ.get("TAVILY_API_KEY", "")
    if not api_key:
        return []
    try:
        body: dict[str, Any] = {
            "api_key": api_key, "query": query,
            "max_results": 5, "search_depth": "basic",
        }
        # Freshness is asked for at request time because Tavily's results
        # carry no publish date to score afterwards (verified live
        # 2026-08-28: fields are title/url/content/score/raw_content/id).
        # Without the window the standing query's top-5 was an evergreen
        # set; with "week" the same query returned five different, dated-
        # this-week pages.
        window = _search_time_range()
        if window:
            body["time_range"] = window
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            "https://api.tavily.com/search", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        results = data.get("results", []) if isinstance(data, dict) else []
        return [
            {"title": r.get("title", ""), "url": r.get("url", ""),
             "text": r.get("content", "")}
            for r in results if isinstance(r, dict)
        ]
    except Exception as e:
        logger.debug("agent_reach: tavily search %r failed: %s", query, e)
        return []


def _fetch_search_results(query: str) -> list[dict[str, Any]]:
    """Backend chain: mcporter/Exa (Agent-Reach's native route) if installed,
    else Tavily REST if a key is set, else the channel is off."""
    if _mcporter_available():
        return _fetch_exa_results(query)
    if os.environ.get("TAVILY_API_KEY"):
        return _fetch_tavily_results(query)
    return []


# ------------------------------------------------------------------ scanner --

def scan_agent_reach() -> list[dict[str, Any]]:
    """Return `reach_web_update` / `reach_search_result` triggers across the
    configured watch URLs and search queries."""
    watch_urls = _csv_env("ZUGAMIND_REACH_WATCH_URLS")
    queries = _csv_env("ZUGAMIND_REACH_QUERIES")
    if not watch_urls and not queries:
        return []  # unconfigured = off

    ttl = int(os.environ.get("ZUGAMIND_REACH_CACHE_TTL", str(_DEFAULT_CACHE_TTL)))
    search_ttl = int(os.environ.get("ZUGAMIND_REACH_SEARCH_TTL", str(_DEFAULT_SEARCH_TTL)))
    cache = _load_json(_FETCH_CACHE_FILE, {"ts": 0, "search_ts": 0, "watch_hashes": {}})
    now = time.time()
    run_watch = bool(watch_urls) and now - cache.get("ts", 0) >= ttl
    run_search = bool(queries) and now - cache.get("search_ts", 0) >= search_ttl
    if not run_watch and not run_search:
        return []  # both channels inside their cadence; fetches cost latency/spend

    watch_hashes: dict[str, str] = dict(cache.get("watch_hashes", {}))
    watch_lines: dict[str, list[str]] = dict(cache.get("watch_lines", {}))
    seen: set[str] = set(_load_json(_SEEN_FILE, []))

    # Each candidate carries what it takes to commit itself to disk *after*
    # the cap is applied, so overflow candidates are left uncommitted (and
    # therefore still eligible next cycle) instead of being marked done and
    # lost forever.
    candidates: list[tuple[str, str, str | None, list[str] | None, dict[str, Any]]] = []

    for url in (watch_urls if run_watch else []):
        body = _fetch_jina(url)
        if body is None:
            continue
        digest = hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]
        line_keys = [_line_key(l) for l in _content_lines(body)][-_WATCH_LINES_MAX:]
        previous = watch_hashes.get(url)
        previous_lines = watch_lines.get(url)
        if previous is None or previous_lines is None:
            # First sight: baseline silently. Committed immediately (not cap-
            # gated) since there's no trigger competing for a slot here. A
            # cache written before the line-set existed re-baselines the same
            # silent way rather than reporting the whole page as "new".
            watch_hashes[url] = digest
            watch_lines[url] = line_keys
            continue
        if digest == previous:
            continue
        added = _added_lines(body, previous_lines)
        if not added:
            # The bytes moved but nothing was ADDED — a reorder, a removal, a
            # rotating promo slot. Advance the baseline silently: reporting
            # this as a signal is what let a page with no new headline buy a
            # real session (2026-08-18).
            watch_hashes[url] = digest
            watch_lines[url] = line_keys
            continue
        added_text = " | ".join(added)
        candidates.append(("watch", url, digest, line_keys, {
            "type": "reach_web_update",
            # The DIFF, not the top of the page. The old head-of-body detail
            # was the same skip-link boilerplate on every fire, so neither the
            # mind nor a woken session could tell a new headline from a footer
            # tweak.
            "detail": f"Watched page changed: {url} -- +{len(added)} new: {added_text}"[:280],
            "novelty": min(0.85, 0.55 + 0.05 * len(added)),
            # Scored on what CHANGED, not on the whole page. Scoring the body
            # pinned a keyword-rich page at the 0.9 relevance ceiling forever:
            # any byte-change to anthropic.com/news bid a fixed 0.25 + 0.4*0.9
            # + 0.2*0.3 = 0.67 against a 0.600 wake floor, so that one URL was
            # a standing wake voucher regardless of what it published.
            "relevance": _keyword_relevance(added_text[:5000]),
            "urgency": 0.3,
            "url": url,
        }))

    if run_search:
        for query in queries:
            for r in _fetch_search_results(query):
                if not isinstance(r, dict):
                    continue
                url = r.get("url", "")
                key = f"{query}:{url}"
                # Absolute URLs only: some backends surface aggregator
                # redirect paths like /goto?url=... -- a relative link is
                # useless as a briefing citation and junk in the seen-set.
                if not url.startswith("http") or key in seen:
                    continue
                title = r.get("title", url)
                candidates.append(("search", key, None, None, {
                    "type": "reach_search_result",
                    "detail": f"[{query}] {title} -- {url}"[:280],
                    "novelty": 0.6,
                    # Scored on the keywords the query did NOT already
                    # contain — the ones a result could actually reveal.
                    "relevance": _keyword_relevance(f"{title} {r.get('text', '')}", exclude=query),
                    # Publish age when the backend reports one (Exa's
                    # `publishedDate`), low when it does not (Tavily).
                    "urgency": _search_urgency(_parse_published(r.get("publishedDate")), now),
                    "url": url,
                    "query": query,
                }))

    emitted = candidates[:_MAX_TRIGGERS]
    triggers: list[dict[str, Any]] = []
    for kind, key, digest, line_keys, trigger in emitted:
        triggers.append(trigger)
        if kind == "watch":
            watch_hashes[key] = digest
            if line_keys is not None:
                watch_lines[key] = line_keys
        else:
            seen.add(key)

    if run_watch:
        cache["ts"] = now
    if run_search:
        cache["search_ts"] = now
    cache["watch_hashes"] = watch_hashes
    cache["watch_lines"] = watch_lines
    _save_json(_FETCH_CACHE_FILE, cache)
    _save_json(_SEEN_FILE, sorted(seen)[-_SEEN_MAX:])
    return triggers
