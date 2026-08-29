"""GitHub repo-events scanner — your project's own social + release signal.

Watches the repos in ZUGAMIND_WATCH_REPOS (same env the github_issues scanner
uses; unset = scanner off) and emits a trigger when a repo's world-visible
state CHANGES: stars gained, forks gained, a new release published. The
motivating gap: a repo milestone (e.g. crossing 10 stars) is exactly the kind
of event an always-on attention layer exists to notice, and none of the
shipped scanners saw it.

Self-deduping by construction: the previous counts live in the cache file and
a trigger only ever emits for a DELTA between consecutive scans, so neither
habituation nor extra_scanners injection can double-fire it. (This matters —
caller-injected extra_scanners bypass habituation by design, and a deployment
may inject this scanner before the file lands in a commit for dynamic
discovery. Same function name in both paths means the runner's scanner dict
dedupes to one instance.)

Env:
    ZUGAMIND_WATCH_REPOS   comma-separated "owner/repo" list. Required.
    GITHUB_TOKEN           optional; raises rate limits / allows private repos.

Star milestones (10/25/50/100/250/500/1k/2.5k/5k/10k) get boosted urgency —
crossing one is a moment the operator plausibly wants a wake for; a routine
+1 star is ambient good news.

Stdlib only, fail-silent, disk-cached (ZUGAMIND_DATA_DIR honored) — per the
scanner contract in scanners/__init__.py.

2026-08-29 audit fixes:

5. Adopts safe_http.fetch_json for both endpoints this scanner polls per
   repo (repo info + latest release), so an unchanged repo answers 304 with
   no body -- which on GitHub does not count against the rate limit -- and a
   rate-limited repo backs off instead of being retried into a 403 every
   pass. On a 304 the previously observed stars/forks/release fields are
   reused (they're already sitting in `cache["repos"][repo]`, this
   scanner's own diff baseline) instead of re-fetching a body that would
   just say the same thing.
7. `cache["repos"]` (and the new per-URL `cache["feeds"]`) is pruned of any
   repo no longer in ZUGAMIND_WATCH_REPOS. Before this, a removed repo kept
   its last-known counts forever, and re-adding it later diffed against
   that stale baseline -- one giant fake "+N stars"/"+N forks" trigger for
   everything gained while it sat unwatched, instead of the silent baseline
   a repo is supposed to get the first time it's ever observed.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from scanners.seen_items import atomic_write_text
from scanners import safe_http

logger = logging.getLogger("zugamind.scanners.github_repo_events")

_REPO_API = "https://api.github.com/repos/{repo}"
_RELEASE_API = "https://api.github.com/repos/{repo}/releases/latest"
_TIMEOUT = 8.0
_SCAN_TTL = float(os.environ.get("ZUGAMIND_REPO_EVENTS_TTL", "900"))  # 15 min
_DATA_DIR = Path(os.environ.get("ZUGAMIND_DATA_DIR")
                 or Path(__file__).resolve().parent.parent.parent / "data")
_CACHE_FILE = _DATA_DIR / "scanner_cache" / "github_repo_events.json"

_MILESTONES = (10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)


def _watched_repos() -> list[str]:
    raw = os.environ.get("ZUGAMIND_WATCH_REPOS", "")
    return [r.strip() for r in raw.split(",") if r.strip()]


def _fetch(url: str, state: dict[str, Any], name: str) -> tuple:
    """One conditional GET through the shared helper. See safe_http.fetch_json
    for the (status, data) contract; `state` is the per-URL etag/last_modified/
    blocked_until/fails dict the CALLER persists (here: under cache["feeds"])."""
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # fetch_json already uses safe_http.opener() internally, so a Bearer
    # token added here still can't follow a redirect off api.github.com.
    return safe_http.fetch_json(url, state=state, headers=headers,
                                timeout=_TIMEOUT, name=name)


def _repo_state(repo: str, prev: "dict[str, Any] | None",
                feed_state: dict[str, Any]) -> "dict[str, Any] | None":
    """Current world-visible state of one repo, or None if the repo endpoint
    could not be read at all (fresh 200 needed, or nothing to fall back to).

    `feed_state` holds this repo's two per-URL safe_http state dicts (one for
    the repo endpoint, one for the release endpoint) — persisted by the
    caller across sweeps so conditional GET actually has something to send.
    A 304 on either endpoint reuses the matching field(s) from `prev`, since
    "unchanged" is exactly what `prev` already records.
    """
    prev = prev or {}
    repo_status, data = _fetch(_REPO_API.format(repo=repo),
                               feed_state.setdefault("repo", {}),
                               name=f"repo_events:{repo}:repo")
    if repo_status == "ok" and isinstance(data, dict) and "stargazers_count" in data:
        stars = int(data.get("stargazers_count") or 0)
        forks = int(data.get("forks_count") or 0)
    elif repo_status == "not_modified":
        stars = int(prev.get("stars") or 0)
        forks = int(prev.get("forks") or 0)
    else:
        return None  # repo endpoint down and nothing reusable — try again next pass

    state = {
        "stars": stars,
        "forks": forks,
        "release_id": prev.get("release_id"),
        "release_tag": prev.get("release_tag", ""),
    }

    rel_status, rel = _fetch(_RELEASE_API.format(repo=repo),
                             feed_state.setdefault("release", {}),
                             name=f"repo_events:{repo}:release")
    if rel_status == "ok" and isinstance(rel, dict) and rel.get("id"):
        state["release_id"] = rel["id"]
        state["release_tag"] = str(rel.get("tag_name") or "")[:60]
    # "not_modified": release fields already carried over from `prev` above.
    # "failed"/"rate_limited" (e.g. a repo with zero releases 404s forever):
    # same — a release-endpoint blip must not erase a previously known release.
    return state


def _crossed_milestone(prev: int, cur: int) -> int | None:
    """Highest milestone crossed moving prev -> cur, or None."""
    crossed = [m for m in _MILESTONES if prev < m <= cur]
    return crossed[-1] if crossed else None


def diff_state(repo: str, prev: dict | None, cur: dict) -> list[dict]:
    """Pure diff: triggers for what changed between two observed states.

    First observation of a repo (prev None) emits NOTHING — it baselines.
    Otherwise a founder-repo scanner would fire a fake '+N stars' trigger on
    every fresh deployment.
    """
    if not prev:
        return []
    out: list[dict] = []

    d_stars = cur["stars"] - int(prev.get("stars") or 0)
    if d_stars > 0:
        milestone = _crossed_milestone(int(prev.get("stars") or 0), cur["stars"])
        out.append({
            "type": "repo_star_delta",
            "detail": (f"{repo} crossed {milestone} stars (now {cur['stars']}, +{d_stars})"
                       if milestone else
                       f"{repo} gained {d_stars} star(s) (now {cur['stars']})"),
            "id": f"{repo}:stars:{cur['stars']}",
            "repo": repo,
            "stars": cur["stars"],
            "novelty": 0.85,
            "relevance": 0.85,
            "urgency": 0.6 if milestone else 0.4,
        })

    d_forks = cur["forks"] - int(prev.get("forks") or 0)
    if d_forks > 0:
        out.append({
            "type": "repo_fork",
            "detail": f"{repo} forked (+{d_forks}, now {cur['forks']}) — someone is building on it",
            "id": f"{repo}:forks:{cur['forks']}",
            "repo": repo,
            "forks": cur["forks"],
            "novelty": 0.9,
            "relevance": 0.85,
            "urgency": 0.5,
        })

    if cur.get("release_id") and cur["release_id"] != prev.get("release_id"):
        out.append({
            "type": "repo_release",
            "detail": f"{repo} published release {cur.get('release_tag') or cur['release_id']}",
            "id": f"{repo}:release:{cur['release_id']}",
            "repo": repo,
            "novelty": 0.8,
            "relevance": 0.8,
            "urgency": 0.45,
        })
    return out


def _load_cache() -> dict:
    try:
        if _CACHE_FILE.exists():
            data = json.loads(_CACHE_FILE.read_text("utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.debug("repo_events cache load failed (ignoring): %s", e)
    return {}


def _save_cache(cache: dict) -> None:
    try:
        atomic_write_text(_CACHE_FILE, json.dumps(cache))
    except Exception as e:
        logger.debug("repo_events cache save failed (non-fatal): %s", e)


def scan_github_repo_events() -> list[dict]:
    """Return triggers for star/fork/release changes on watched repos."""
    repos = _watched_repos()
    if not repos:
        return []

    now = time.time()
    cache = _load_cache()
    if (now - float(cache.get("ts") or 0)) < _SCAN_TTL:
        return []  # scanned recently; deltas already emitted that pass

    triggers: list[dict] = []
    states = cache.setdefault("repos", {})
    feeds = cache.setdefault("feeds", {})

    # Prune anything no longer watched BEFORE diffing, so a re-added repo
    # baselines silently (like a brand-new one) instead of diffing against
    # a count frozen from whenever it was dropped. See fix 7 in the module
    # docstring.
    watched = set(repos)
    for stale in [r for r in states if r not in watched]:
        del states[stale]
    for stale in [r for r in feeds if r not in watched]:
        del feeds[stale]

    for repo in repos:
        feed_state = feeds.setdefault(repo, {})
        cur = _repo_state(repo, states.get(repo), feed_state)
        if cur is None:
            continue  # fetch failed — keep prev state, try again next pass
        triggers.extend(diff_state(repo, states.get(repo), cur))
        states[repo] = cur

    cache["ts"] = now
    _save_cache(cache)
    return triggers


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for t in scan_github_repo_events():
        print(t["detail"])
