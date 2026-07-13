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
    retries: int = 1,
) -> str | None:
    """Query the local Ollama model. Returns response text or None on error.

    If `system` is non-empty, prepended as a system message in the chat history.

    A cancelled/timed-out load can leave the Ollama scheduler briefly wedged
    (observed 2026-07-11: a client-side timeout mid-load left the server
    refusing all subsequent connections until manually restarted). One retry
    after a short pause recovers from that transient state without needing a
    server restart; a genuinely down Ollama still returns None after both
    attempts, same as before.
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
                time.sleep(3)
    return None


def ollama_available() -> bool:
    """Check if Ollama is running and the local model is loaded."""
    try:
        req = Request(f"{OLLAMA_URL}/api/tags")
        resp = urlopen(req, timeout=5)
        data = json.loads(resp.read().decode())
        models = [m.get("name", "") for m in data.get("models", [])]
        return any(LOCAL_MODEL.split(":")[0] in m for m in models)
    except Exception:
        return False
