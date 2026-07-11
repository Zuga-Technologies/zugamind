"""Claude API client — Anthropic /v1/messages with prompt caching.

Used when the workspace's action gate decides a cycle warrants escalating
beyond the local Sentinel tier (e.g. a complex deliberation, a stuck loop, a
high-stakes decision). Direct urllib call — bypasses the SDK so this module
keeps ZugaMind's stdlib-only discipline.

The 5-minute prompt cache is enabled by sending the system block with
`cache_control: {"type": "ephemeral"}`. Repeated calls with the same system
prompt get a cache hit and pay reduced input cost.

Authentication: `ANTHROPIC_API_KEY` env var only. Returns None if unset — the
caller is expected to handle that gracefully.
"""

import json
import logging
import os
from urllib.request import Request, urlopen

logger = logging.getLogger("zugamind.models.claude")


def _load_anthropic_key() -> str | None:
    """Read ANTHROPIC_API_KEY from the environment."""
    return os.environ.get("ANTHROPIC_API_KEY")


def query_claude_api(
    prompt: str,
    model: str,
    max_tokens: int = 500,
    system: str = "",
) -> str | None:
    """Query Claude API directly via urllib. Returns response text or None.

    If `system` is non-empty, sent as a prompt-cached system block. The
    cache_control marker enables Anthropic's 5-minute prompt cache.
    """
    api_key = _load_anthropic_key()
    if not api_key:
        logger.warning("No ANTHROPIC_API_KEY found — cannot call Claude API")
        return None

    try:
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        # Claude Sonnet 5+ runs adaptive thinking when `thinking` is omitted;
        # at this module's small max_tokens the whole budget goes to thinking
        # and the response carries no text block. Disable it — these are
        # short judgment calls, not reasoning tasks. (Fable/Mythos reject an
        # explicit disable; thinking is always on there, so omit instead.)
        if "fable" not in model and "mythos" not in model:
            body["thinking"] = {"type": "disabled"}
        if system.strip():
            body["system"] = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        payload = json.dumps(body).encode()
        req = Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
        resp = urlopen(req, timeout=30)
        data = json.loads(resp.read().decode())
        content = data.get("content", [])
        for block in content:
            if block.get("type") == "text" and block.get("text"):
                return block["text"]
        logger.warning(
            "Claude API returned no text block (model=%s, stop_reason=%s, blocks=%s)",
            model, data.get("stop_reason"), [b.get("type") for b in content],
        )
        return None
    except Exception as e:
        logger.warning("Claude API call failed (%s): %s", model, e)
        return None
