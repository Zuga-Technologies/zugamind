"""Ollama client — local-only LLM calls for the fast Sentinel tier.

`ollama_query` is a single-pass chat completion. Returns text or None on
error. Used for cycle decisions against the configured `LOCAL_MODEL`.

`ollama_available` is the probe that verifies Ollama is up and the
configured model is INSTALLED (downloaded — `/api/tags`; it does not prove
the weights are loaded, a cold load happens on the first real call).
`value_gate` runs it before each of its queries; nothing runs it at boot
today.

Stdlib only — urllib.request, no aiohttp/httpx.

Audit 2026-08-28 — what changed and why:
- CONTEXT WINDOW. No `num_ctx` was sent, so Ollama used the model's default
  (2048-4096 tokens) and SILENTLY dropped the front of any longer prompt —
  which is the system message, i.e. the judge's instructions. Measured on
  this box: a ~14,500-token prompt evaluated 2,050 tokens (half of the
  4096 default) and the model answered without ever seeing the rule in the
  system block; `num_ctx: 8192` still read only 4,098 (half again); at
  16384 all 14,569 were read and the rule was obeyed. The wake-decision
  path sends a JSON dump of the winner + plan with no size cap. `num_ctx`
  is now sized from the prompt (conservative 2 chars/token) plus `max_tokens` and
  headroom, in powers of two from 4096 up to `OLLAMA_MAX_CTX`, and the
  response's `prompt_eval_count` is checked for Ollama's truncation
  signature (it keeps exactly half the window) so truncation is at
  least loud when the cap is hit.
- RETRIES. Every exception was retried 3x with 15s of sleep, including a
  deterministic HTTP 400. Connection errors, timeouts and 5xx still get the
  full retry (the 2026-07-11 wedged-scheduler and 2026-08-03 transient-404
  incidents below are real); 404 keeps ONE retry for the model-swap case;
  400/401/403/422 return at once.
- ERROR BODIES. `str(HTTPError)` is "HTTP Error 404: Not Found"; the body
  says "model 'x' not found, try pulling it first". The body is logged now.
- EMPTY ANSWERS. A 200 with no `message.content` returned "" — which the
  action gate treated as a SUCCESSFUL decision with an empty payload. Now
  None. `done_reason == "length"` (hit num_predict) is logged.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from foundation.config import LOCAL_MODEL, OLLAMA_MAX_CTX, OLLAMA_URL, SENTINEL_TIMEOUT

logger = logging.getLogger("zugamind.models.ollama")

OLLAMA_MIN_CTX = 4096
_CHARS_PER_TOKEN = 2       # conservative on purpose: measured 2.1 chars/token on a hyphen-and-
                           # number-heavy prompt (JSON dumps of winner+plan are similar);
                           # English prose is ~4, so the window is often 2x what it needs —
                           # that costs RAM once per size step, a too-small window costs the
                           # system message silently
_CTX_HEADROOM_TOKENS = 256
_NO_RETRY_STATUSES = {400, 401, 403, 405, 413, 422}


def estimate_tokens(text: str) -> int:
    return len(text) // _CHARS_PER_TOKEN + 1


def num_ctx_for(prompt_tokens: int, max_tokens: int) -> int:
    """Smallest power-of-two window >= prompt + answer + headroom, clamped to
    [OLLAMA_MIN_CTX, OLLAMA_MAX_CTX]. Powers of two keep the loaded model's
    KV cache stable across calls instead of reloading for every new size."""
    need = prompt_tokens + max(0, int(max_tokens)) + _CTX_HEADROOM_TOKENS
    ctx = OLLAMA_MIN_CTX
    while ctx < need and ctx < OLLAMA_MAX_CTX:
        ctx *= 2
    return min(ctx, OLLAMA_MAX_CTX)


def _http_error_detail(e: HTTPError) -> str:
    try:
        raw = e.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""
    try:
        return str(json.loads(raw).get("error", raw))[:300]
    except Exception:  # noqa: BLE001
        return raw[:300]


def ollama_query(
    prompt: str,
    model: str = LOCAL_MODEL,
    max_tokens: int = 500,
    system: str = "",
    timeout: int = SENTINEL_TIMEOUT,
    keep_alive: str = "10m",
    retries: int = 3,
) -> Optional[str]:
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
    (2026-08-28: a 404 now gets ONE retry — that covers a model swap in
    flight — while a model that is simply not installed stops costing 15s of
    sleep per cycle; 400-class errors never retry.)
    """
    messages = []
    if system.strip():
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    prompt_tokens = estimate_tokens(system) + estimate_tokens(prompt)
    num_ctx = num_ctx_for(prompt_tokens, max_tokens)
    if prompt_tokens + max_tokens + _CTX_HEADROOM_TOKENS > OLLAMA_MAX_CTX:
        logger.warning(
            "Ollama prompt (~%d tokens + %d answer) exceeds OLLAMA_MAX_CTX=%d — Ollama will "
            "drop the FRONT of the prompt (the system message). Raise ZUGAMIND_OLLAMA_MAX_CTX "
            "or shorten the prompt.", prompt_tokens, max_tokens, OLLAMA_MAX_CTX,
        )

    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "stream": False,
            "keep_alive": keep_alive,
            "options": {"num_predict": max_tokens, "temperature": 0.3, "num_ctx": num_ctx},
        }
    ).encode("utf-8")

    attempts = max(1, retries + 1)
    retried_404 = False
    for attempt in range(attempts):
        is_last = attempt == attempts - 1
        try:
            req = Request(
                f"{OLLAMA_URL}/api/chat",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except HTTPError as e:
            detail = _http_error_detail(e)
            if e.code in _NO_RETRY_STATUSES:
                logger.warning("Ollama query rejected (HTTP %d, model=%s): %s", e.code, model, detail)
                return None
            if e.code == 404 and retried_404:
                logger.warning("Ollama query failed (HTTP 404, model=%s, not transient): %s", model, detail)
                return None
            if e.code == 404:
                retried_404 = True
            logger.warning("Ollama query failed (attempt %d/%d, HTTP %d, model=%s): %s",
                           attempt + 1, attempts, e.code, model, detail)
            if is_last:
                return None
            time.sleep(3 + attempt * 2)  # 3s, 5s, 7s -- more room per retry
            continue
        except (URLError, OSError, TimeoutError, ValueError) as e:  # refused, timeout, bad JSON
            logger.warning("Ollama query failed (attempt %d/%d): %s", attempt + 1, attempts, e)
            if is_last:
                return None
            time.sleep(3 + attempt * 2)
            continue
        except Exception as e:  # noqa: BLE001 — never raise out of the Sentinel tier
            logger.warning("Ollama query failed (attempt %d/%d): %s", attempt + 1, attempts, e)
            if is_last:
                return None
            time.sleep(3 + attempt * 2)
            continue

        return _extract_answer(data, model, num_ctx, max_tokens, prompt_tokens)
    return None


def _extract_answer(data: Any, model: str, num_ctx: int, max_tokens: int,
                    prompt_tokens: int) -> Optional[str]:
    if not isinstance(data, dict):
        logger.warning("Ollama returned a non-object body (model=%s)", model)
        return None
    message = data.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        logger.warning("Ollama returned no answer text (model=%s, done_reason=%s) — treating as a "
                       "failed call, not an empty decision", model, data.get("done_reason"))
        return None

    # Truncation signature, measured live 2026-08-28 (qwen2.5:3b): when the
    # prompt overflows the window Ollama keeps exactly HALF of it — 2050 of a
    # 4096 window, 4098 of 8192 — while a full read lands anywhere below.
    evaluated = data.get("prompt_eval_count")
    half = num_ctx // 2
    if isinstance(evaluated, int) and half <= evaluated <= half + 16:
        logger.warning(
            "Ollama likely TRUNCATED the prompt: evaluated %d tokens = half of num_ctx=%d "
            "(estimated ~%d) — the system message goes first. Raise ZUGAMIND_OLLAMA_MAX_CTX "
            "or shorten the prompt.", evaluated, num_ctx, prompt_tokens,
        )
    if data.get("done_reason") == "length":
        logger.warning("Ollama answer hit num_predict=%d (model=%s) — the answer is incomplete",
                       max_tokens, model)
    return content


def _same_tag(a: str, b: str) -> bool:
    """Compare two Ollama model refs, treating a bare name as ':latest'."""
    def norm(s: str) -> str:
        return s if ":" in s else f"{s}:latest"
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

    "Installed" means downloaded (`/api/tags`), not loaded into memory: the
    first real call after a restart still pays the cold load, which for a
    14B model can take most of `SENTINEL_TIMEOUT` on its own.
    """
    try:
        req = Request(f"{OLLAMA_URL}/api/tags")
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        models = [m.get("name", "") for m in data.get("models", []) if isinstance(m, dict)]
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
    except Exception:  # noqa: BLE001 — Ollama down is a False, never a crash
        return False


__all__ = ["ollama_query", "ollama_available", "estimate_tokens", "num_ctx_for", "OLLAMA_MIN_CTX"]
