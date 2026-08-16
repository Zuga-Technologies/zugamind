"""Ollama client — local-only LLM calls for the fast Sentinel tier.

`ollama_query` is a single-pass chat completion. Returns text or None on
error. Used for cycle decisions against the configured `LOCAL_MODEL`.

`ollama_available` is the health probe used at boot to verify the model is
actually loaded into Ollama before the cycle starts polling.

Stdlib only — urllib.request, no aiohttp/httpx.
"""

import json
import logging
import time
from urllib.request import Request, urlopen

from foundation.config import LOCAL_MODEL, OLLAMA_URL, SENTINEL_TIMEOUT

logger = logging.getLogger("zugamind.models.ollama")


def ollama_query(
    prompt: str,
    model: str = LOCAL_MODEL,
    max_tokens: int = 500,
    system: str = "",
    timeout: int = SENTINEL_TIMEOUT,
    keep_alive: str = "10m",
    retries: int = 3,
) -> str | None:
    """Query the local Ollama model. Returns response text or None on error.

    If `system` is non-empty, prepended as a system message in the chat history.

    A cancelled/timed-out load can leave the Ollama scheduler briefly wedged
    (observed 2026-07-11: a client-side timeout mid-load left the server
    refusing all subsequent connections until manually restarted). One retry
    after a short pause recovers from that transient state without needing a
    server restart; a genuinely down Ollama still returns None after both
    attempts, same as before.

    Bumped 1->3 retries + backoff (2026-08-03): a real repo_issues alarm hit
    two straight transient 404s from Ollama and got dropped as harness_skip
    even though a manual retry moments later succeeded clean. Two attempts
    wasn't enough margin for a real event to survive a brief hiccup.
    """
    messages = []
    if system.strip():
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "stream": False,
            "keep_alive": keep_alive,
            "options": {"num_predict": max_tokens, "temperature": 0.3},
        }
    ).encode()

    attempts = max(1, retries + 1)
    for attempt in range(attempts):
        try:
            req = Request(
                f"{OLLAMA_URL}/api/chat",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urlopen(req, timeout=timeout)
            data = json.loads(resp.read().decode())
            return data.get("message", {}).get("content", "")
        except Exception as e:
            is_last = attempt == attempts - 1
            logger.warning(
                "Ollama query failed (attempt %d/%d): %s", attempt + 1, attempts, e
            )
            if not is_last:
                time.sleep(3 + attempt * 2)  # 3s, 5s, 7s -- more room per retry
    return None


def _same_tag(a: str, b: str) -> bool:
    """Compare two Ollama model refs, treating a bare name as ':latest'."""
    norm = lambda s: s if ":" in s else f"{s}:latest"
    return norm(a.strip()) == norm(b.strip())


def ollama_available() -> bool:
    """Check if Ollama is running and the configured model is installed.

    Matches the FULL tag, not the family prefix. It used to compare only
    `LOCAL_MODEL.split(":")[0]` — so a configured `qwen2.5:14b-instruct`
    was reported "available" on a box that only had `qwen2.5:7b-instruct`,
    because the family name `qwen2.5` appeared in the list. The boot probe
    passed and then every single real call 404'd with "model not found"
    (BugaPC, 2026-08-16, issue #22: the daemon looked healthy and silently
    stopped journalling for hours). A probe that checks a weaker condition
    than the thing it guards is worse than no probe -- it converts a loud
    startup failure into a silent runtime one.
    """
    try:
        req = Request(f"{OLLAMA_URL}/api/tags")
        resp = urlopen(req, timeout=5)
        data = json.loads(resp.read().decode())
        models = [m.get("name", "") for m in data.get("models", [])]
        if any(_same_tag(LOCAL_MODEL, m) for m in models):
            return True
        logger.error(
            "Configured LOCAL_MODEL %r is NOT installed in Ollama. Installed: %s. "
            "Every query will 404 until this is pulled or ZUGAMIND_LOCAL_MODEL is "
            "pointed at an installed tag.",
            LOCAL_MODEL,
            ", ".join(models) or "(none)",
        )
        return False
    except Exception:
        return False
