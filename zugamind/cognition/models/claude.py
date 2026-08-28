"""Claude API client — Anthropic /v1/messages with prompt caching.

Used when the workspace's action gate decides a cycle warrants escalating
beyond the local Sentinel tier (e.g. a complex deliberation, a stuck loop, a
high-stakes decision). Direct urllib call — bypasses the SDK so this module
keeps ZugaMind's stdlib-only discipline.

The 5-minute prompt cache is enabled by sending the system block with
`cache_control: {"type": "ephemeral"}`. Repeated calls with the same system
prompt get a cache hit and pay reduced input cost — once the system prompt
is longer than the model's minimum cacheable prefix (512-4096 tokens
depending on model); a shorter one silently just doesn't cache.

Authentication: `ANTHROPIC_API_KEY` env var only. Returns None if unset — the
caller is expected to handle that gracefully. The key never reaches a log
line: error paths log the API's own error type/message, never headers.

Audit 2026-08-28 — what changed and why:
- Retries. A single 429/529/5xx or a timeout used to return None on the
  first try, which the gate reports as api_error and the runner turns into
  a skipped wake — the local tier got 3 attempts, the paid one got 0. Now
  the retryable statuses get `_MAX_RETRIES` attempts with backoff, honoring
  `Retry-After`; 400/401/403/404 never retry.
- Error bodies. `str(HTTPError)` is "HTTP Error 400: Bad Request"; the JSON
  body names the rejected parameter. It is logged now.
- `stop_reason`. "refusal" (HTTP 200, safety classifier) and "max_tokens"
  (truncated) were indistinguishable from a network failure. Refusals log
  their category and return None; truncation is logged and flagged.
- Usage. `usage_out` hands the caller the real token counts, the model that
  answered, the stop reason and a USD cost from the price table, so the
  budget can record what was actually spent instead of a flat per-tier
  guess (which priced a cache hit the same as a cache miss).
- `thinking`. Only Sonnet 5 and Opus 5 run adaptive thinking when the
  parameter is omitted — and at this module's small max_tokens the whole
  budget went to thinking and no text came back — so ONLY those two get an
  explicit disable. Everything else (Haiku 4.5, Opus 4.8/4.7, the 4.6s)
  runs without thinking when the parameter is omitted, and Fable/Mythos
  reject any explicit disable; both just omit it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from foundation.config import REASONING_TIMEOUT

logger = logging.getLogger("zugamind.models.claude")

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
_MAX_RETRIES = 2                       # the SDK's default
_RETRY_STATUSES = {408, 409, 429, 500, 502, 503, 504, 529}
_MAX_BACKOFF_SEC = 30.0

# Models that run adaptive thinking when `thinking` is OMITTED. Those need an
# explicit disable for short judgment calls; every other model runs without
# thinking when the parameter is absent, and Fable/Mythos reject a disable.
_ADAPTIVE_WHEN_OMITTED = ("claude-sonnet-5", "claude-opus-5")

# USD per million tokens (input, output), Anthropic first-party rates cached
# 2026-06-24. Cache reads bill at 0.1x input, cache writes at 1.25x. Matched
# by prefix so a dated alias ("claude-haiku-4-5-20251001") prices like its
# family. Override/extend with ZUGAMIND_CLAUDE_PRICES='{"claude-x": [in, out]}'.
_PRICES_PER_MTOK: Dict[str, Tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def _load_anthropic_key() -> Optional[str]:
    """Read ANTHROPIC_API_KEY from the environment."""
    return os.environ.get("ANTHROPIC_API_KEY")


def _prices() -> Dict[str, Tuple[float, float]]:
    table = dict(_PRICES_PER_MTOK)
    raw = os.environ.get("ZUGAMIND_CLAUDE_PRICES")
    if raw:
        try:
            for k, v in json.loads(raw).items():
                table[str(k)] = (float(v[0]), float(v[1]))
        except Exception as e:  # noqa: BLE001 — a bad override must not break a call
            logger.warning("ZUGAMIND_CLAUDE_PRICES ignored (malformed): %s", e)
    return table


def estimate_cost_usd(model: str, usage: Dict[str, Any]) -> Optional[float]:
    """USD for one response from its `usage` block, or None for a model not
    in the price table (the caller falls back to its flat estimate)."""
    price = None
    for prefix, rates in sorted(_prices().items(), key=lambda kv: -len(kv[0])):
        if model == prefix or model.startswith(prefix + "-") or model.startswith(prefix):
            price = rates
            break
    if price is None:
        return None
    inp, out = price

    def n(key: str) -> float:
        v = usage.get(key, 0)
        return float(v) if isinstance(v, (int, float)) else 0.0

    total = (n("input_tokens") * inp
             + n("cache_read_input_tokens") * inp * 0.1
             + n("cache_creation_input_tokens") * inp * 1.25
             + n("output_tokens") * out)
    return round(total / 1_000_000, 6)


def _thinking_param(model: str) -> Optional[Dict[str, str]]:
    if model.startswith(_ADAPTIVE_WHEN_OMITTED):
        return {"type": "disabled"}
    return None


def _error_body(e: HTTPError) -> Tuple[Optional[str], str]:
    """(error.type, error.message) from an API error body — the only place
    the API says WHICH parameter it rejected. Never includes request headers."""
    try:
        raw = e.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None, ""
    try:
        err = json.loads(raw).get("error") or {}
        return err.get("type"), str(err.get("message", ""))[:300]
    except Exception:  # noqa: BLE001
        return None, raw[:300]


def _retry_delay(e: Optional[HTTPError], attempt: int) -> float:
    if e is not None:
        ra = e.headers.get("retry-after") if e.headers else None
        try:
            if ra is not None:
                return min(float(ra), _MAX_BACKOFF_SEC)
        except ValueError:
            pass
    return min(1.0 * (2 ** attempt), _MAX_BACKOFF_SEC)


def query_claude_api(
    prompt: str,
    model: str,
    max_tokens: int = 500,
    system: str = "",
    usage_out: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Query Claude API directly via urllib. Returns response text or None.

    If `system` is non-empty, sent as a prompt-cached system block.
    If `usage_out` is given it is filled with `model`, `usage`, `stop_reason`,
    `cost_usd` (None for an unpriced model), `attempts` and `truncated`.
    Never raises.
    """
    api_key = _load_anthropic_key()
    if not api_key:
        logger.warning("No ANTHROPIC_API_KEY found — cannot call Claude API")
        return None

    try:
        body: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        thinking = _thinking_param(model)
        if thinking is not None:
            body["thinking"] = thinking
        if system.strip():
            body["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
        payload = json.dumps(body).encode("utf-8")
        headers = {
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
    except Exception as e:  # noqa: BLE001 — never raise out of this function
        logger.warning("Claude API request could not be built (%s): %s", model, e)
        return None

    data: Optional[Dict[str, Any]] = None
    attempt = 0
    while True:
        try:
            req = Request(API_URL, data=payload, headers=headers, method="POST")
            with urlopen(req, timeout=REASONING_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            break
        except HTTPError as e:
            err_type, err_msg = _error_body(e)
            if e.code in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                delay = _retry_delay(e, attempt)
                logger.warning("Claude API HTTP %d %s (model=%s), retry %d/%d in %.1fs: %s",
                               e.code, err_type or "", model, attempt + 1, _MAX_RETRIES, delay, err_msg)
                time.sleep(delay)
                attempt += 1
                continue
            logger.warning("Claude API call failed (model=%s, HTTP %d %s): %s",
                           model, e.code, err_type or "", err_msg)
            return None
        except (URLError, OSError, TimeoutError) as e:  # connection, DNS, socket timeout
            if attempt < _MAX_RETRIES:
                delay = _retry_delay(None, attempt)
                logger.warning("Claude API unreachable (model=%s), retry %d/%d in %.1fs: %s",
                               model, attempt + 1, _MAX_RETRIES, delay, e)
                time.sleep(delay)
                attempt += 1
                continue
            logger.warning("Claude API call failed (model=%s): %s", model, e)
            return None
        except Exception as e:  # noqa: BLE001 — malformed body etc.; never raise
            logger.warning("Claude API call failed (model=%s): %s", model, e)
            return None

    if not isinstance(data, dict):
        logger.warning("Claude API returned a non-object body (model=%s)", model)
        return None

    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    stop_reason = data.get("stop_reason")
    if usage_out is not None:
        usage_out.update({
            "model": data.get("model") or model,
            "usage": usage,
            "stop_reason": stop_reason,
            "cost_usd": estimate_cost_usd(str(data.get("model") or model), usage),
            "attempts": attempt + 1,
            "truncated": stop_reason == "max_tokens",
        })

    if stop_reason == "refusal":
        details = data.get("stop_details") if isinstance(data.get("stop_details"), dict) else {}
        logger.warning("Claude API refused the request (model=%s, category=%s): %s",
                       model, details.get("category"), str(details.get("explanation", ""))[:200])
        return None

    content = data.get("content") or []
    text = "".join(b.get("text", "") for b in content
                   if isinstance(b, dict) and b.get("type") == "text")
    if not text:
        logger.warning(
            "Claude API returned no text block (model=%s, stop_reason=%s, blocks=%s)",
            model, stop_reason, [b.get("type") for b in content if isinstance(b, dict)],
        )
        return None
    if stop_reason == "max_tokens":
        logger.warning("Claude API output truncated at max_tokens=%s (model=%s) — "
                       "the answer is incomplete", max_tokens, model)
    return text


__all__ = ["query_claude_api", "estimate_cost_usd"]
