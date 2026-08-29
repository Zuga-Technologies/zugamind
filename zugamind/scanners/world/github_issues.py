"""GitHub issues scanner — watches configured repos for new open issues.

The flagship "point it at your repo" sense: new issues become `repo_issue`
triggers that can win the workspace and wake your harness to triage them
while you sleep.

Configuration (env):
    ZUGAMIND_WATCH_REPOS   comma-separated "owner/repo" list. Unset/empty
                           means this scanner is OFF and returns [].
    GITHUB_TOKEN           optional; raises the API rate limit and allows
                           watching private repos.

An issue triggers on every sweep for as long as it is open and has ZERO
comments — the world state is the dedupe. The moment anyone (the woken
harness included) comments, the trigger stops on its own. This makes the
perceive->wake->act loop self-extinguishing: acting on the trigger is what
silences it. Pull requests are excluded (the issues API returns them too).

Stdlib only. Failure-silent per scanner contract. Cached 4 min on disk.

2026-08-29 audit fixes, all proved by code that ran before the fix:

1. A cache file that is valid JSON but the WRONG SHAPE (e.g. a stray `[]`)
   used to raise on the very next line, BEFORE any successful fetch got a
   chance to overwrite it -- a permanent kill, not a transient one, because
   every sweep re-read the same file and died the same way. `_load_cache`
   now validates the shape and deletes a bad file so the NEXT sweep starts
   cold instead of repeating the crash forever, and every read off a cached
   row goes through `.get()` / `safe_http.num` instead of bare indexing.
2. `_MAX_TRIGGERS` used to cap over `cache["items"]` in repo order with no
   interleaving, so one repo with 5+ stale uncommented issues starved every
   other watched repo forever. `_round_robin_by_repo` (same idea as
   ai_labs._round_robin, keyed on repo instead of lab) fixes that.
3. A per-repo fetch failure used to `continue` and then let
   `cache["items"] = items` overwrite the WHOLE list, so that repo's
   untriaged issues vanished for the whole TTL on a single blip. Failures
   now fall back to that repo's previous items instead of dropping them.
4. Adopts safe_http.fetch_json for conditional GET: on GitHub a 304 does
   not count against the rate limit at all, and a rate-limited repo now
   backs off instead of being retried into a 403 every sweep.
5. A GitHub issue title is third-party text that becomes literally part of
   the briefing handed to a paid model. It is sanitized (control/whitespace
   collapsed, truncated at a word boundary) before it ever reaches `detail`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from foundation.fs import atomic_write_text
from foundation.text_format import truncate_title
from .. import safe_http

logger = logging.getLogger("zugamind.scanners.github_issues")

_TIMEOUT = 8.0
_CACHE_TTL = 240
_MAX_TRIGGERS = 5
# Honors ZUGAMIND_DATA_DIR without importing foundation — scanners stay standalone.
_DATA_DIR = Path(os.environ.get("ZUGAMIND_DATA_DIR") or Path(__file__).resolve().parent.parent.parent / "data")
_CACHE_DIR = _DATA_DIR / "scanner_cache"
# Kept as a module attribute for back-compat with tests that monkeypatch it
# directly (tests/scanners/test_github_issues.py) — but nothing below reads
# this name. `_cache_file()` recomputes from `_CACHE_DIR` on every call
# instead, exactly like ai_labs.py's `_cache_file()`/`_seen_file()`: a
# `_CACHE_FILE = _CACHE_DIR / "..."` bound once at import time keeps pointing
# at the pre-patch location forever, because patching `_CACHE_DIR` afterwards
# (which is all tests/conftest.py's autouse fixture does) can't retroactively
# change an already-computed Path. That gap let tests read and overwrite the
# LIVE deployment's cache file while believing they were isolated.
_CACHE_FILE = _CACHE_DIR / "github_issues.json"
_API = "https://api.github.com/repos/{repo}/issues?state=open&sort=created&direction=desc&per_page=10"

# Collapses any run of whitespace or C0/DEL control characters (a literal
# newline included) to one space. An issue title is written by anyone who
# can open an issue on the watched repo, and it lands verbatim in `detail`,
# which is exactly the text a paid model is briefed with -- an embedded
# newline is a free way to inject a fake extra line into that briefing.
_CONTROL_WS_RE = re.compile(r"[\s\x00-\x1f\x7f]+")

# Per-URL safe_http state (etag/last_modified/blocked_until/fails) plus the
# last good raw payload, keyed by repo. Bridges `_fetch_issues(repo)` (kept
# single-argument so existing tests can still monkeypatch it with a plain
# `lambda repo: [...]`) to the persisted "feeds" section of the cache file
# without changing that call signature. Populated from disk at the top of
# `scan_github_issues` and written back at the end.
_FEEDS_STATE: dict[str, dict[str, Any]] = {}


def _watched_repos() -> list[str]:
    raw = os.environ.get("ZUGAMIND_WATCH_REPOS", "")
    return [r.strip() for r in raw.split(",") if r.strip()]


def _sanitize_title(title: Any) -> str:
    """Third-party text, made safe for a model's briefing: whitespace/control
    runs collapsed to one space, then cut at a word boundary. See the module
    docstring, fix 5."""
    collapsed = _CONTROL_WS_RE.sub(" ", str(title or "")).strip()
    return truncate_title(collapsed, limit=160)


def _cache_file() -> Path:
    """Resolved fresh on every call, not bound at import — same trap and
    same fix as ai_labs._cache_file(). `_CACHE_FILE` above stays as an inert
    module attribute only so an existing monkeypatch of it doesn't
    AttributeError; this is what `_load_cache`/`_save_cache` actually use."""
    return _CACHE_DIR / "github_issues.json"


def _round_robin_by_repo(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Interleave cached issues per repo so one repo can't monopolize
    _MAX_TRIGGERS. Same idea as ai_labs._round_robin, keyed on repo instead
    of lab -- see the module docstring, fix 2."""
    by_repo: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        by_repo.setdefault(it.get("repo", "?"), []).append(it)
    ordered: list[dict[str, Any]] = []
    while any(by_repo.values()):
        for repo_items in by_repo.values():
            if repo_items:
                ordered.append(repo_items.pop(0))
    return ordered


def _load_cache() -> dict[str, Any]:
    default: dict[str, Any] = {"ts": 0, "items": []}
    path = _cache_file()
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
            # Valid JSON, wrong shape -- e.g. a stray `[]`. Before this
            # check the very next `cache.get(...)` call raised, and that
            # raise happened BEFORE any successful fetch could overwrite
            # the file, so the same bad file killed every future sweep too.
            # Delete it so the NEXT sweep starts cold instead of repeating
            # the crash forever.
            logger.warning(
                "github_issues cache at %s is valid JSON but not a dict "
                "(got %s) -- discarding it so the next sweep starts cold",
                path, type(data).__name__,
            )
            try:
                path.unlink()
            except OSError:
                pass
    except Exception as e:
        logger.debug("github_issues cache load failed: %s", e)
    return default


def _save_cache(cache: dict[str, Any]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_text(_cache_file(), json.dumps(cache))
    except Exception as e:
        logger.debug("github_issues cache save failed: %s", e)


def _fetch_issues(repo: str) -> list[dict[str, Any]]:
    """Raw open issues for `repo` (GitHub's own shape), or raise on failure.

    Goes through safe_http.fetch_json for conditional GET: an unchanged repo
    then costs a 304 and no body, and on GitHub a 304 does NOT count against
    the rate limit. On a 304 the last good raw payload (kept in `_FEEDS_STATE`,
    persisted under cache["feeds"]) is returned so the caller's PR/comment
    filtering runs the same way whether the body was fresh or reused.
    """
    state = _FEEDS_STATE.setdefault(repo, {})
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, data = safe_http.fetch_json(
        _API.format(repo=repo), state=state, headers=headers,
        timeout=_TIMEOUT, name=f"github_issues:{repo}",
    )
    if status == "ok":
        raw = data if isinstance(data, list) else []
        state["raw"] = raw
        return raw
    if status == "not_modified":
        prev_raw = state.get("raw")
        return prev_raw if isinstance(prev_raw, list) else []
    # "rate_limited" or "failed": raise so the caller's existing per-repo
    # except/continue keeps this repo's PREVIOUS triaged items (fix 3)
    # instead of reading "fetch problem" as "repo has zero open issues".
    raise RuntimeError(f"github_issues: fetch failed for {repo} ({status})")


def scan_github_issues() -> list[dict[str, Any]]:
    """Return `repo_issue` triggers for open, UNCOMMENTED issues on watched repos."""
    repos = _watched_repos()
    if not repos:
        return []

    cache = _load_cache()
    if time.time() - safe_http.num(cache.get("ts")) > _CACHE_TTL:
        feeds = cache.get("feeds")
        _FEEDS_STATE.clear()
        _FEEDS_STATE.update(feeds if isinstance(feeds, dict) else {})

        # Grouped so a failing repo can fall back to what it had last time
        # instead of vanishing from `items` for the whole TTL (fix 3).
        prev_by_repo: dict[str, list[dict[str, Any]]] = {}
        for it in cache.get("items", []):
            if isinstance(it, dict):
                prev_by_repo.setdefault(it.get("repo", ""), []).append(it)

        items: list[dict[str, Any]] = []
        for repo in repos:
            try:
                repo_items: list[dict[str, Any]] = []
                for issue in _fetch_issues(repo):
                    if not isinstance(issue, dict):
                        continue
                    if "pull_request" in issue:
                        continue
                    if issue.get("comments", 0) > 0:
                        continue  # already triaged — the world state is the dedupe
                    repo_items.append({
                        "id": issue.get("id"),
                        "repo": repo,
                        "number": issue.get("number"),
                        "title": _sanitize_title(issue.get("title")),
                        "url": issue.get("html_url", ""),
                        "author": (issue.get("user") or {}).get("login", "?"),
                    })
                items.extend(repo_items)
            except Exception as e:
                logger.debug("github_issues fetch %s failed: %s", repo, e)
                items.extend(prev_by_repo.get(repo, []))  # keep prev state
        cache["ts"] = time.time()
        cache["items"] = items
        cache["feeds"] = dict(_FEEDS_STATE)
        _save_cache(cache)

    cached_items = [it for it in cache.get("items", []) if isinstance(it, dict) and it.get("id") is not None]

    triggers: list[dict[str, Any]] = []
    for it in _round_robin_by_repo(cached_items):
        triggers.append({
            "type": "repo_issue",
            "detail": f"Untriaged issue #{it.get('number')} on {it.get('repo')}: {it.get('title')}",
            "novelty": 0.9,
            "relevance": 0.8,
            "urgency": 0.5,
            "issue_id": it.get("id"),
            "issue_number": it.get("number"),
            "issue_title": it.get("title"),
            "issue_url": it.get("url"),
            "issue_author": it.get("author"),
            "repo": it.get("repo"),
        })
        if len(triggers) >= _MAX_TRIGGERS:
            break
    return triggers
