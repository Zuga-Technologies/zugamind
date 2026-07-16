"""Example private scanner — Discord channel activity.

Not part of the ZugaMind package; a worked example of the extra_scanners
pattern documented in examples/custom-scanners/README.md. Copy this file
into your own deployment (it never needs to live inside zugamind/) and pass
it to StreamRunner(extra_scanners={"scan_discord_activity": scan_discord_activity}).

Watches ONE Discord channel via the bot REST API (GET .../messages) for new
messages, and turns unseen ones into triggers. Dedupe is "seen message id"
persisted to disk — once a message has produced a trigger it will not fire
again, even across restarts.

Configuration (env):
    DISCORD_BOT_TOKEN         bot token with Read Message History on the
                               target channel.
    ZUGAMIND_DISCORD_CHANNEL  channel ID to watch (right-click the channel
                               in Discord with Developer Mode on -> Copy ID).
    ZUGAMIND_DISCORD_MENTION  optional: only trigger on messages containing
                               this string, case-insensitive. Unset = any
                               new message in the channel triggers.

Unset DISCORD_BOT_TOKEN or ZUGAMIND_DISCORD_CHANNEL -> scanner is off, returns [].
Stdlib only (urllib.request). Fail-silent per scanner contract.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger("zugamind.examples.discord_activity")

_TIMEOUT = 8.0
_CACHE_TTL = 30  # seconds — Discord history is cheap to poll
_MAX_TRIGGERS = 5
_DATA_DIR = Path(os.environ.get("ZUGAMIND_DATA_DIR") or Path(__file__).resolve().parent / "data")
_CACHE_DIR = _DATA_DIR / "scanner_cache"
_SEEN_FILE = _CACHE_DIR / "discord_activity_seen.json"
_API = "https://discord.com/api/v10/channels/{channel}/messages"


def _load_seen() -> set[str]:
    try:
        if _SEEN_FILE.exists():
            return set(json.loads(_SEEN_FILE.read_text(encoding="utf-8")))
    except Exception as e:
        logger.debug("discord_activity seen-cache load failed: %s", e)
    return set()


def _save_seen(seen: set[str]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        trimmed = sorted(seen)[-500:]
        _SEEN_FILE.write_text(json.dumps(trimmed), encoding="utf-8")
    except Exception as e:
        logger.debug("discord_activity seen-cache save failed: %s", e)


def _fetch_recent(channel: str, token: str) -> list[dict[str, Any]]:
    url = _API.format(channel=channel) + "?limit=20"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ZugaMind/example-scanner",
            "Authorization": f"Bot {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def scan_discord_activity() -> list[dict[str, Any]]:
    """Return `discord_message` triggers for unseen messages in the configured channel."""
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    channel = os.environ.get("ZUGAMIND_DISCORD_CHANNEL", "").strip()
    if not token or not channel:
        return []
    mention = os.environ.get("ZUGAMIND_DISCORD_MENTION", "").lower().strip()

    try:
        messages = _fetch_recent(channel, token)
    except Exception as e:
        logger.debug("discord_activity fetch failed: %s", e)
        return []

    if not isinstance(messages, list):
        logger.debug("discord_activity API error: %s", messages)
        return []

    seen = _load_seen()
    triggers: list[dict[str, Any]] = []
    newly_seen: set[str] = set()

    for msg in messages:
        mid = msg.get("id")
        content = msg.get("content") or ""
        author = (msg.get("author") or {}).get("username", "?")
        if not mid or mid in seen:
            continue
        if mention and mention not in content.lower():
            continue
        newly_seen.add(mid)
        if not content:
            continue  # embed/attachment-only messages: mark seen, don't trigger
        triggers.append({
            "type": "discord_message",
            "detail": f"#{channel} — {author}: {content[:200]}",
            "novelty": 0.8,
            "relevance": 0.7,
            "urgency": 0.4,
            "channel": channel,
            "message_id": mid,
            "author": author,
        })
        if len(triggers) >= _MAX_TRIGGERS:
            break

    if newly_seen:
        _save_seen(seen | newly_seen)

    return triggers
