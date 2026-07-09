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
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger("zugamind.scanners.github_issues")

_TIMEOUT = 8.0
_CACHE_TTL = 240
_MAX_TRIGGERS = 5
# Honors ZUGAMIND_DATA_DIR without importing foundation — scanners stay standalone.
_DATA_DIR = Path(os.environ.get("ZUGAMIND_DATA_DIR") or Path(__file__).resolve().parent.parent.parent / "data")
_CACHE_DIR = _DATA_DIR / "scanner_cache"
_CACHE_FILE = _CACHE_DIR / "github_issues.json"
_API = "https://api.github.com/repos/{repo}/issues?state=open&sort=created&direction=desc&per_page=10"


def _watched_repos() -> list[str]:
    raw = os.environ.get("ZUGAMIND_WATCH_REPOS", "")
    return [r.strip() for r in raw.split(",") if r.strip()]


def _load_cache() -> dict[str, Any]:
    try:
        if _CACHE_FILE.exists():
            return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("github_issues cache load failed: %s", e)
    return {"ts": 0, "items": []}


def _save_cache(cache: dict[str, Any]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(cache), encoding="utf-8")
    except Exception as e:
        logger.debug("github_issues cache save failed: %s", e)


def _fetch_issues(repo: str) -> list[dict[str, Any]]:
    req = urllib.request.Request(
        _API.format(repo=repo),
        headers={
            "User-Agent": "ZugaMind/scanner",
            "Accept": "application/vnd.github+json",
        },
    )
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    return data if isinstance(data, list) else []


def scan_github_issues() -> list[dict[str, Any]]:
    """Return `repo_issue` triggers for open, UNCOMMENTED issues on watched repos."""
    repos = _watched_repos()
    if not repos:
        return []

    cache = _load_cache()
    if time.time() - cache.get("ts", 0) > _CACHE_TTL:
        items: list[dict[str, Any]] = []
        for repo in repos:
            try:
                for issue in _fetch_issues(repo):
                    if "pull_request" in issue:
                        continue
                    if issue.get("comments", 0) > 0:
                        continue  # already triaged — the world state is the dedupe
                    items.append({
                        "id": issue.get("id"),
                        "repo": repo,
                        "number": issue.get("number"),
                        "title": (issue.get("title") or "")[:160],
                        "url": issue.get("html_url", ""),
                        "author": (issue.get("user") or {}).get("login", "?"),
                    })
            except Exception as e:
                logger.debug("github_issues fetch %s failed: %s", repo, e)
        cache["ts"] = time.time()
        cache["items"] = items
        _save_cache(cache)

    triggers: list[dict[str, Any]] = []
    for it in cache.get("items", []):
        if it.get("id") is None:
            continue
        triggers.append({
            "type": "repo_issue",
            "detail": f"Untriaged issue #{it['number']} on {it['repo']}: {it['title']}",
            "novelty": 0.9,
            "relevance": 0.8,
            "urgency": 0.5,
            "issue_id": it["id"],
            "issue_number": it["number"],
            "issue_title": it["title"],
            "issue_url": it["url"],
            "issue_author": it["author"],
            "repo": it["repo"],
        })
        if len(triggers) >= _MAX_TRIGGERS:
            break
    return triggers
