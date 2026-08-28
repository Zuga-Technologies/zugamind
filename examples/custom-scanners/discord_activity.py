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
import time
import urllib.error
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
_FETCH_CACHE_FILE = _CACHE_DIR / "discord_activity_fetch.json"
_API = "https://discord.com/api/v10/channels/{channel}/messages"


def _load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("discord_activity cache load failed (%s): %s", path.name, e)
    return default


def _save_json(path: Path, payload: Any) -> None:
    # Write-then-rename so a crash mid-write can't leave a half-written,
    # unparseable JSON file behind (os.replace is atomic on both POSIX and
    # Windows when src/dst share a filesystem, which they do here).
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        logger.debug("discord_activity cache save failed (%s): %s", path.name, e)


# Seen-set eviction (changed 2026-08-22) — see news_rss.py for the full
# rationale: {id: last_seen_epoch} with refresh-on-observe + TTL eviction
# (Stripe-style, by age not count) replaces the old `sorted(ids)[-500:]`
# trim. _SEEN_MAX is a memory backstop only, oldest-first. Legacy bare-list
# files load fine.
_SEEN_TTL_SECONDS = 30 * 86400
_SEEN_MAX = 5000


def _load_seen() -> dict[str, float]:
    now = time.time()
    try:
        if _SEEN_FILE.exists():
            raw = json.loads(_SEEN_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, list):  # legacy format: bare id list
                return {str(i): now for i in raw}
            if isinstance(raw, dict):
                out: dict[str, float] = {}
                for k, v in raw.items():
                    try:
                        out[str(k)] = float(v)
                    except (TypeError, ValueError):
                        out[str(k)] = now
                return out
    except Exception as e:
        logger.debug("discord_activity seen-cache load failed: %s", e)
    return {}


def _save_seen(seen: dict[str, float], now: float) -> None:
    cutoff = now - _SEEN_TTL_SECONDS
    kept = {k: v for k, v in seen.items() if v >= cutoff}
    if len(kept) > _SEEN_MAX:
        kept = dict(sorted(kept.items(), key=lambda kv: kv[1])[-_SEEN_MAX:])
    _save_json(_SEEN_FILE, kept)


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

    # Fetch-level cache: poll the API at most once per _CACHE_TTL, not once
    # per scan call (the scanner contract requires this — was previously
    # unwired despite _CACHE_TTL existing, so every scan hit Discord live).
    fetch_cache = _load_json(_FETCH_CACHE_FILE, {"ts": 0, "messages": []})
    now0 = time.time()
    if now0 - fetch_cache.get("ts", 0) > _CACHE_TTL:
        try:
            messages = _fetch_recent(channel, token)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # Respect Discord's rate-limit window instead of hammering it
                # again next cycle: Retry-After is seconds (Discord sends it
                # as a float). Push the cache's clock forward so the normal
                # TTL check above stays false until the window passes.
                try:
                    retry_after = float(e.headers.get("Retry-After", _CACHE_TTL))
                except (TypeError, ValueError):
                    retry_after = _CACHE_TTL
                fetch_cache["ts"] = now0 + retry_after - _CACHE_TTL
                _save_json(_FETCH_CACHE_FILE, fetch_cache)
            logger.debug("discord_activity fetch failed: HTTP %s", e.code)
            messages = fetch_cache.get("messages", [])
        except Exception as e:
            logger.debug("discord_activity fetch failed: %s", e)
            messages = fetch_cache.get("messages", [])
        else:
            if isinstance(messages, list):
                fetch_cache = {"ts": now0, "messages": messages}
                _save_json(_FETCH_CACHE_FILE, fetch_cache)
            else:
                logger.debug("discord_activity API error: %s", messages)
                messages = fetch_cache.get("messages", [])
    else:
        messages = fetch_cache.get("messages", [])

    seen = _load_seen()
    now = time.time()
    triggers: list[dict[str, Any]] = []
    newly_seen: set[str] = set()
    touched = False

    for msg in messages:
        mid = msg.get("id")
        content = msg.get("content") or ""
        author = (msg.get("author") or {}).get("username", "?")
        if not mid:
            continue
        is_new = mid not in seen
        seen[mid] = now  # refresh-on-observe
        touched = True
        if not is_new:
            continue
        if mention and mention not in content.lower():
            continue
        newly_seen.add(mid)
        if not content:
            continue  # embed/attachment-only messages: mark seen, don't trigger
        # Collapse newlines/tabs so `detail` stays one line (Discord content
        # can contain raw \n), then hard-cap at the contract's 280 chars —
        # component limits (32-char username, snowflake width) keep this
        # under 280 today, but don't rely on Discord's limits never changing.
        flat_content = " ".join(content.split())[:200]
        detail = f"#{channel} — {author}: {flat_content}"[:280]
        triggers.append({
            "type": "discord_message",
            "detail": detail,
            "novelty": 0.8,
            "relevance": 0.7,
            "urgency": 0.4,
            "channel": channel,
            "message_id": mid,
            "author": author,
        })
        if len(triggers) >= _MAX_TRIGGERS:
            break

    if touched:
        _save_seen(seen, now)

    return triggers
