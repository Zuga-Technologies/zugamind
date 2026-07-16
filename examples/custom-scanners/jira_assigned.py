"""Example private scanner — Jira issues assigned to you.

Not part of the ZugaMind package; a worked example of the extra_scanners
pattern documented in examples/custom-scanners/README.md. Copy this file
into your own deployment and pass it to
StreamRunner(extra_scanners={"scan_jira_assigned": scan_jira_assigned}).

Watches one Jira Cloud project via the REST API for issues assigned to a
configured account that are not yet in a "Done"-category status, and turns
each into a trigger. The world state (Jira's own status field) is the
dedupe: once an issue moves to Done it stops triggering, the same
self-extinguishing pattern scanners/world/github_issues.py uses.

Configuration (env):
    JIRA_BASE_URL              e.g. https://yourorg.atlassian.net
    JIRA_EMAIL                 the account email for basic auth.
    JIRA_API_TOKEN             API token (id.atlassian.com/manage-profile/security/api-tokens).
    ZUGAMIND_JIRA_PROJECT      project key to scope the search, e.g. "ENG".

Unset any of JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN -> scanner is off,
returns []. Stdlib only (urllib.request, base64). Fail-silent per scanner
contract. Cached 5 min on disk (Jira search is a heavier call than Slack
history — no reason to hit it every cycle on a short interval).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger("zugamind.examples.jira_assigned")

_TIMEOUT = 8.0
_CACHE_TTL = 300
_MAX_TRIGGERS = 5
_DATA_DIR = Path(os.environ.get("ZUGAMIND_DATA_DIR") or Path(__file__).resolve().parent / "data")
_CACHE_DIR = _DATA_DIR / "scanner_cache"
_CACHE_FILE = _CACHE_DIR / "jira_assigned.json"


def _load_cache() -> dict[str, Any]:
    try:
        if _CACHE_FILE.exists():
            return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("jira_assigned cache load failed: %s", e)
    return {"ts": 0, "items": []}


def _save_cache(cache: dict[str, Any]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(cache), encoding="utf-8")
    except Exception as e:
        logger.debug("jira_assigned cache save failed: %s", e)


def _fetch_assigned(base_url: str, email: str, token: str, project: str) -> list[dict[str, Any]]:
    jql = f'project = "{project}" AND assignee = currentUser() AND statusCategory != Done'
    query = urllib.parse.urlencode({
        "jql": jql,
        "maxResults": 10,
        "fields": "summary,status,updated",
    })
    url = f"{base_url.rstrip('/')}/rest/api/3/search?{query}"
    auth = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ZugaMind/example-scanner",
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    return data.get("issues", [])


def scan_jira_assigned() -> list[dict[str, Any]]:
    """Return `jira_assigned` triggers for open issues assigned to the configured user."""
    base_url = os.environ.get("JIRA_BASE_URL", "").strip()
    email = os.environ.get("JIRA_EMAIL", "").strip()
    token = os.environ.get("JIRA_API_TOKEN", "").strip()
    project = os.environ.get("ZUGAMIND_JIRA_PROJECT", "").strip()
    if not (base_url and email and token and project):
        return []

    cache = _load_cache()
    if time.time() - cache.get("ts", 0) > _CACHE_TTL:
        try:
            issues = _fetch_assigned(base_url, email, token, project)
        except Exception as e:
            logger.debug("jira_assigned fetch failed: %s", e)
            issues = cache.get("items", [])  # keep serving stale data over a transient error
        else:
            cache["ts"] = time.time()
            cache["items"] = issues
            _save_cache(cache)

    triggers: list[dict[str, Any]] = []
    for issue in cache.get("items", [])[:_MAX_TRIGGERS]:
        key = issue.get("key")
        fields = issue.get("fields") or {}
        if not key:
            continue
        triggers.append({
            "type": "jira_assigned",
            "detail": f"{key} ({fields.get('status', {}).get('name', '?')}): "
                      f"{(fields.get('summary') or '')[:180]}",
            "novelty": 0.6,
            "relevance": 0.8,
            "urgency": 0.4,
            "issue_key": key,
            "issue_status": (fields.get("status") or {}).get("name", "?"),
            "issue_url": f"{base_url.rstrip('/')}/browse/{key}",
        })

    return triggers
