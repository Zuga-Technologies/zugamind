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
     only if the hash actually moved.
  2. search — Exa web search via `mcporter call exa.web_search_exa`, run as
     a subprocess. Requires `mcporter` (github.com/mcporter) on PATH; if it
     isn't installed this channel is silently off — not an error, just one
     fewer set of eyes, same as the other channel being unconfigured.

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
    ZUGAMIND_REACH_CACHE_TTL    seconds between re-fetching either channel.
                                Default 3600 (60 min) — kept high by design,
                                Jina Reader renders + Exa search both cost
                                real latency and, for Exa, real API spend
                                upstream of mcporter; this is not tuned for
                                sub-minute freshness.
    ZUGAMIND_REACH_KEYWORDS     optional comma-separated keywords, case-
                                insensitive. If set, relevance scores higher
                                for hits and lower for misses; if unset,
                                every trigger gets a neutral 0.5 relevance.

Dedupe: web_watch keys off a per-URL content hash persisted to disk; search
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
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger("zugamind.examples.agent_reach")

_TIMEOUT = 20.0  # Jina Reader renders + mcporter subprocess both run slower than a bare API call
_DEFAULT_CACHE_TTL = 3600
_MAX_TRIGGERS = 5
_SEEN_MAX = 1000
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


def _keyword_relevance(text: str) -> float:
    """Deterministic relevance from the optional keyword list (no LLM,
    no network) — same shape as every other scanner in this directory."""
    keywords = _csv_env("ZUGAMIND_REACH_KEYWORDS")
    if not keywords:
        return 0.5
    hits = sum(1 for k in keywords if k.lower() in text.lower())
    return min(0.9, 0.3 + 0.2 * hits) if hits else 0.2


# ---------------------------------------------------------------- web_watch --

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


# ------------------------------------------------------------------ scanner --

def scan_agent_reach() -> list[dict[str, Any]]:
    """Return `reach_web_update` / `reach_search_result` triggers across the
    configured watch URLs and search queries."""
    watch_urls = _csv_env("ZUGAMIND_REACH_WATCH_URLS")
    queries = _csv_env("ZUGAMIND_REACH_QUERIES")
    if not watch_urls and not queries:
        return []  # unconfigured = off

    ttl = int(os.environ.get("ZUGAMIND_REACH_CACHE_TTL", str(_DEFAULT_CACHE_TTL)))
    cache = _load_json(_FETCH_CACHE_FILE, {"ts": 0, "watch_hashes": {}})
    if time.time() - cache.get("ts", 0) < ttl:
        return []  # respect the fetch cadence; both channels cost real latency/spend

    watch_hashes: dict[str, str] = dict(cache.get("watch_hashes", {}))
    seen: set[str] = set(_load_json(_SEEN_FILE, []))

    # Each candidate carries what it takes to commit itself to disk *after*
    # the cap is applied, so overflow candidates are left uncommitted (and
    # therefore still eligible next cycle) instead of being marked done and
    # lost forever.
    candidates: list[tuple[str, str, str | None, dict[str, Any]]] = []

    for url in watch_urls:
        body = _fetch_jina(url)
        if body is None:
            continue
        digest = hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]
        previous = watch_hashes.get(url)
        if previous is None:
            # First sight: baseline silently. Committed immediately (not cap-
            # gated) since there's no trigger competing for a slot here.
            watch_hashes[url] = digest
            continue
        if digest == previous:
            continue
        head = " ".join(body.split())[:160]
        candidates.append(("watch", url, digest, {
            "type": "reach_web_update",
            "detail": f"Watched page changed: {url} -- {head}"[:280],
            "novelty": 0.7,
            "relevance": _keyword_relevance(body[:5000]),
            "urgency": 0.3,
            "url": url,
        }))

    if queries and _mcporter_available():
        for query in queries:
            for r in _fetch_exa_results(query):
                if not isinstance(r, dict):
                    continue
                url = r.get("url", "")
                key = f"{query}:{url}"
                if not url or key in seen:
                    continue
                title = r.get("title", url)
                candidates.append(("search", key, None, {
                    "type": "reach_search_result",
                    "detail": f"[{query}] {title} -- {url}"[:280],
                    "novelty": 0.6,
                    "relevance": _keyword_relevance(f"{title} {r.get('text', '')}"),
                    "urgency": 0.2,
                    "url": url,
                    "query": query,
                }))

    emitted = candidates[:_MAX_TRIGGERS]
    triggers: list[dict[str, Any]] = []
    for kind, key, digest, trigger in emitted:
        triggers.append(trigger)
        if kind == "watch":
            watch_hashes[key] = digest
        else:
            seen.add(key)

    cache["ts"] = time.time()
    cache["watch_hashes"] = watch_hashes
    _save_json(_FETCH_CACHE_FILE, cache)
    _save_json(_SEEN_FILE, sorted(seen)[-_SEEN_MAX:])
    return triggers
