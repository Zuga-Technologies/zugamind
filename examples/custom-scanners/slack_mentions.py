"""Example private scanner — Slack mentions.

Not part of the ZugaMind package; a worked example of the extra_scanners
pattern documented in examples/custom-scanners/README.md. Copy this file
into your own deployment (it never needs to live inside zugamind/) and pass
it to StreamRunner(extra_scanners={"scan_slack_mentions": scan_slack_mentions}).

Watches ONE Slack channel via the conversations.history Web API for messages
containing a configured mention string, and turns unseen ones into triggers.
Dedupe is "seen message ts" persisted to disk — once a message has produced a
trigger it will not fire again, even across restarts.

Configuration (env):
    SLACK_BOT_TOKEN          bot token with channels:history scope.
    ZUGAMIND_SLACK_CHANNEL   channel ID to watch (not the #name — the ID,
                             e.g. C0123ABCD; find it in the channel's URL
                             or via conversations.list).
    ZUGAMIND_SLACK_MENTION   string to match in message text, case-insensitive.
                             Defaults to "@here" if unset (adjust to your
                             bot's actual @mention or a keyword).

Unset SLACK_BOT_TOKEN or ZUGAMIND_SLACK_CHANNEL -> scanner is off, returns [].
Stdlib only (urllib.request). Fail-silent per scanner contract.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger("zugamind.examples.slack_mentions")

_TIMEOUT = 8.0
_CACHE_TTL = 60  # seconds — Slack history is cheap to poll relative to Jira/GitHub
_MAX_TRIGGERS = 5
_DATA_DIR = Path(os.environ.get("ZUGAMIND_DATA_DIR") or Path(__file__).resolve().parent / "data")
_CACHE_DIR = _DATA_DIR / "scanner_cache"
_SEEN_FILE = _CACHE_DIR / "slack_mentions_seen.json"
_API = "https://slack.com/api/conversations.history"


def _load_seen() -> set[str]:
    try:
        if _SEEN_FILE.exists():
            return set(json.loads(_SEEN_FILE.read_text(encoding="utf-8")))
    except Exception as e:
        logger.debug("slack_mentions seen-cache load failed: %s", e)
    return set()


def _save_seen(seen: set[str]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # Cap the persisted set so it can't grow unbounded across a long-lived deployment.
        trimmed = sorted(seen)[-500:]
        _SEEN_FILE.write_text(json.dumps(trimmed), encoding="utf-8")
    except Exception as e:
        logger.debug("slack_mentions seen-cache save failed: %s", e)


def _fetch_recent(channel: str, token: str) -> list[dict[str, Any]]:
    url = f"{_API}?channel={channel}&limit=20"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ZugaMind/example-scanner",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    if not data.get("ok"):
        logger.debug("slack_mentions API error: %s", data.get("error"))
        return []
    return data.get("messages", [])


def scan_slack_mentions() -> list[dict[str, Any]]:
    """Return `slack_mention` triggers for unseen messages matching the configured mention."""
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = os.environ.get("ZUGAMIND_SLACK_CHANNEL", "").strip()
    if not token or not channel:
        return []
    mention = os.environ.get("ZUGAMIND_SLACK_MENTION", "@here").lower()

    try:
        messages = _fetch_recent(channel, token)
    except Exception as e:
        logger.debug("slack_mentions fetch failed: %s", e)
        return []

    seen = _load_seen()
    triggers: list[dict[str, Any]] = []
    newly_seen: set[str] = set()

    for msg in messages:
        ts = msg.get("ts")
        text = (msg.get("text") or "")
        if not ts or ts in seen:
            continue
        if mention not in text.lower():
            continue
        newly_seen.add(ts)
        triggers.append({
            "type": "slack_mention",
            "detail": f"Mention in {channel}: {text[:200]}",
            "novelty": 0.8,
            "relevance": 0.7,
            "urgency": 0.4,
            "channel": channel,
            "message_ts": ts,
            "author": msg.get("user", "?"),
        })
        if len(triggers) >= _MAX_TRIGGERS:
            break

    if newly_seen:
        _save_seen(seen | newly_seen)

    return triggers
