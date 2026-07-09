"""Ollama client — local-only LLM calls for the fast Sentinel tier.

`ollama_query` is a single-pass chat completion. Returns text or None on
error. Used for cycle decisions against the configured `LOCAL_MODEL`.

`ollama_available` is the health probe used at boot to verify the model is
actually loaded into Ollama before the cycle starts polling.

Stdlib only — urllib.request, no aiohttp/httpx.
"""

import json
import logging
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
) -> str | None:
    """Query the local Ollama model. Returns response text or None on error.

    If `system` is non-empty, prepended as a system message in the chat history.
    """
    try:
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
        logger.warning("Ollama query failed: %s", e)
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
