"""Single doorway from the workspace to Claude (paid tiers).

Fail-closed: any missing/erroring check means no action. Budget-clamped via
foundation.budget. This is the human-veto + hard-cap safety chokepoint
referenced in the project README.

Stdlib-only. The chat/user-facing surfaces of a deployment (if any) should
NOT route through this gate — this doorway is specifically for autonomous,
deliberate actions the agent decides to take on its own.

Test seam: the `_resolve_*` hooks below are module-level so tests can
monkey-patch them without pulling in the full cognitive loop.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any, Literal, TypedDict

logger = logging.getLogger("zugamind.action_gate")

IntentKind = Literal[
    "code_change", "chat_reply", "remediate", "research", "decide", "other"
]


class ActionIntent(TypedDict, total=False):
    kind: IntentKind
    summary: str
    context: dict
    requires_human: bool
    caller: str
    max_tokens: int
    system: str
    tier: str


# Which tier a given intent kind routes to by default. Callers can override
# with an explicit `tier` on the intent.
_KIND_TO_TIER: dict[str, str] = {
    "code_change": "sonnet",
    "decide": "sonnet",
    "remediate": "sonnet",
    "research": "sonnet",
    "chat_reply": "haiku",
    "other": "haiku",
}

# Tier -> model id. Local Ollama has no real "model id" in the Claude sense;
# "local" is a sentinel the local-model call path checks for. Paid tiers use
# dateless model aliases so each tier tracks the current release of its line;
# all three paid tiers here have a matching cost heuristic in budget.py.
TIER_MODELS: dict[str, str] = {
    "local": "local",
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-4-8",
}

# Idempotency cache: prevents double-spend if a caller retries the same
# intent within a short window (e.g. a caller-side retry-on-timeout).
_IDEMPOTENCY_WINDOW_S = 5.0
_idempotency_cache: dict[str, tuple[float, dict]] = {}


# --- Content screen (H7): acute per-intent safety screen --------------------
# Clear-cut misalignment red flags an intent should NEVER carry: prompt
# injection, destructive shell/SQL, force-push, secret exfiltration, and
# attempts to edit/disable the gate's own safety-critical files. This is the
# fail-closed acute safety screen described in the README's safety section.
_SHIELD_BLOCK_PATTERNS = (
    (r"ignore (all |your )?(previous|prior) instructions", "prompt-injection"),
    (r"disregard (your )?(charter|values|guidelines|safety)", "charter-override"),
    (r"\brm\s+-rf\b", "destructive-shell"),
    (r"\bdrop\s+table\b", "destructive-sql"),
    (r"\bdelete\s+from\b", "destructive-sql"),
    (r"git\s+push\b.{0,30}--force", "force-push"),
    (r"(exfiltrat|leak|dump|print)\w*\b.{0,40}"
     r"(secret|credential|api[_ ]?key|token|\.env|password)", "secret-exfil"),
)
# Editing/disabling this repo's OWN safety spine via a model-call intent.
_SHIELD_SPINE_TOKENS = (
    "budget.py", "action_gate.py", "gates/", "charter.md",
)
_SHIELD_MUTATE_VERBS = ("edit", "modify", "disable", "remove", "weaken",
                        "bypass", "rewrite", "delete", "patch")


def screen_intent(intent: dict) -> str | None:
    """Return a block reason if the intent's content is clearly dangerous/
    misaligned, else None (permit). Scans the human-meaningful text fields.
    Stdlib-only."""
    parts = []
    for k in ("summary", "context", "system", "prompt", "kind"):
        v = intent.get(k)
        if isinstance(v, str):
            parts.append(v)
        elif v is not None:
            parts.append(json.dumps(v, default=str))
    blob = " ".join(parts).lower()
    if not blob.strip():
        return None
    for pat, label in _SHIELD_BLOCK_PATTERNS:
        if re.search(pat, blob):
            return label
    for tok in _SHIELD_SPINE_TOKENS:
        if tok.lower() in blob and any(v in blob for v in _SHIELD_MUTATE_VERBS):
            return f"spine-target:{tok}"
    return None


def _resolve_shield():
    """Return a shield content-check callable: (intent) -> reason str | None.

    Fails CLOSED — if the screen itself raises, the intent is BLOCKED, not
    permitted (fail-closed invariant: a missing/erroring gate means no
    action).
    """
    def _check(intent):
        try:
            return screen_intent(intent)
        except Exception as e:  # noqa: BLE001 — fail closed
            return f"shield_error:{e}"
    return _check


# --- Test seams (lazy imports so tests can patch without pulling heavy deps) -

def _resolve_budget_helpers():
    """Return (can_spend, record_spend, load_budget)."""
    from foundation.budget import can_spend, load_budget, record_spend  # noqa: WPS433
    return can_spend, record_spend, load_budget


def _resolve_claude_caller():
    from cognition.models.claude import query_claude_api  # noqa: WPS433
    return query_claude_api


def _resolve_ollama_caller():
    from cognition.models.ollama import ollama_query  # noqa: WPS433
    return ollama_query


# --- Helpers -----------------------------------------------------------------

def _intent_hash(intent: dict) -> str:
    keys = ("kind", "summary", "context", "max_tokens", "system", "tier")
    keyed = {k: intent.get(k) for k in keys if k in intent}
    return hashlib.sha256(
        json.dumps(keyed, sort_keys=True, default=str).encode()
    ).hexdigest()


def _idempotency_lookup(intent: dict) -> dict | None:
    h = _intent_hash(intent)
    entry = _idempotency_cache.get(h)
    if entry is None:
        return None
    if (time.monotonic() - entry[0]) < _IDEMPOTENCY_WINDOW_S:
        return entry[1]
    _idempotency_cache.pop(h, None)
    return None


def _idempotency_store(intent: dict, response: dict) -> None:
    _idempotency_cache[_intent_hash(intent)] = (time.monotonic(), response)


def _build_prompt(intent: dict) -> str:
    summary = intent.get("summary", "")
    context = intent.get("context", {})
    if not context:
        return summary
    return f"{summary}\n\nContext:\n{json.dumps(context, indent=2, default=str)}"


# --- Public API ----------------------------------------------------------------

def escalate_for_action(intent: ActionIntent, *, dry_run: bool = False) -> dict:
    """Single doorway: the workspace decides -> here -> Claude.

    Returns a dict with at least: ok, response, cost, model, reason. May also
    set `tier` and `caller` depending on path taken.

    Fail-closed BEFORE the model call: any exception resolving budget/model
    helpers, a failed can_spend() check, a budget cap hit, or a shield block
    all return ok=False before Claude/Ollama is ever invoked. Nothing
    silently proceeds.

    AFTER the model call succeeds, ok is True even if persisting the spend to
    budget.json fails (retried once) — the response was already paid for, so
    discarding it wouldn't undo that. In that case `budget_persisted` is
    False and `reason` explains why; callers/monitoring should treat that as
    a signal that the monthly cap is temporarily unenforceable, not ignore it.
    """
    intent_d: dict[str, Any] = dict(intent)
    kind = intent_d.get("kind", "other")
    caller = intent_d.get("caller", f"action_gate.{kind}")

    cached = _idempotency_lookup(intent_d)
    if cached is not None:
        logger.info("action_gate: idempotent hit kind=%s caller=%s", kind, caller)
        return {**cached, "from_cache": True}

    # Human veto point: a caller can mark an intent as needing a human. The
    # gate refuses to auto-execute it -- no model call, full stop. Wiring an
    # actual notification (Discord/Slack/email) onto this is left to the
    # deployer; the OSS core just guarantees the refusal.
    if intent_d.get("requires_human"):
        result = {
            "ok": False,
            "response": None,
            "cost": 0.0,
            "model": "deferred",
            "reason": "requires_human_review",
            "caller": caller,
        }
        _idempotency_store(intent_d, result)
        return result

    tier = intent_d.get("tier") or _KIND_TO_TIER.get(kind, "haiku")
    if tier not in TIER_MODELS:
        # Fail toward the cheaper tier, but never silently — a caller asking
        # for a tier this gate can't route deserves a trace in the log.
        logger.warning(
            "action_gate: unknown tier %r on intent (caller=%s) — downgrading to haiku",
            tier, intent_d.get("caller", "unknown"),
        )
        tier = "haiku"

    shield = _resolve_shield()
    shield_reason = shield(intent_d)
    if shield_reason:
        return {
            "ok": False,
            "response": None,
            "cost": 0.0,
            "model": "blocked",
            "reason": f"shield_refused:{shield_reason}",
            "caller": caller,
        }

    try:
        can_spend, record_spend, load_budget = _resolve_budget_helpers()
    except Exception as exc:
        logger.warning("action_gate: budget import failed: %s", exc)
        return {
            "ok": False,
            "response": None,
            "cost": 0.0,
            "model": "none",
            "reason": f"import_error:{exc}",
            "caller": caller,
        }

    model_id = TIER_MODELS[tier]

    if dry_run:
        return {
            "ok": True,
            "response": None,
            "cost": 0.0,
            "model": model_id,
            "reason": "dry_run",
            "tier": tier,
            "caller": caller,
        }

    try:
        budget = load_budget()
    except Exception as exc:
        logger.warning("action_gate: load_budget failed: %s", exc)
        return {
            "ok": False,
            "response": None,
            "cost": 0.0,
            "model": "none",
            "reason": f"budget_error:{exc}",
            "tier": tier,
            "caller": caller,
        }

    try:
        affordable = can_spend(budget, tier)
    except Exception as exc:  # noqa: BLE001 — fail closed, matches load_budget above
        logger.warning("action_gate: can_spend check failed: %s", exc)
        return {
            "ok": False,
            "response": None,
            "cost": 0.0,
            "model": "none",
            "reason": f"can_spend_error:{exc}",
            "tier": tier,
            "caller": caller,
        }

    if not affordable:
        return {
            "ok": False,
            "response": None,
            "cost": 0.0,
            "model": "none",
            "reason": "budget_exhausted",
            "tier": tier,
            "caller": caller,
        }

    try:
        prompt = _build_prompt(intent_d)
        max_tokens = int(intent_d.get("max_tokens", 500))
        system = str(intent_d.get("system", ""))
        if tier == "local":
            ollama_query = _resolve_ollama_caller()
            response_text = ollama_query(prompt, max_tokens=max_tokens, system=system)
        else:
            query_claude_api = _resolve_claude_caller()
            response_text = query_claude_api(
                prompt, model_id, max_tokens=max_tokens, system=system
            )
    except Exception as exc:
        logger.warning("action_gate: api_error kind=%s: %s", kind, exc)
        return {
            "ok": False,
            "response": None,
            "cost": 0.0,
            "model": model_id,
            "reason": f"api_error:{exc}",
            "tier": tier,
            "caller": caller,
        }

    if response_text is None:
        return {
            "ok": False,
            "response": None,
            "cost": 0.0,
            "model": model_id,
            "reason": "api_error",
            "tier": tier,
            "caller": caller,
        }

    # The model call above already succeeded — real money is already spent on
    # the provider's side. record_spend() persists that fact to budget.json so
    # the NEXT call's can_spend() check sees it. If that persist silently
    # failed and we just shrugged, the on-disk balance would never reflect
    # this spend: every subsequent call reloads budget.json fresh (see
    # load_budget() above), so a single failed write here quietly and
    # invisibly disables the monthly cap for the rest of the day, no
    # concurrency race required. One retry absorbs a transient I/O blip;
    # if it still fails we keep the (already-succeeded) response — discarding
    # a paid-for answer would be wasteful, not "safer" — but we report the
    # persistence failure loudly instead of pretending nothing happened.
    spent_before = float(budget.get("spent", 0.0))
    new_budget = budget
    persist_exc: Exception | None = None
    for attempt in range(2):
        try:
            new_budget = record_spend(budget, tier)
            persist_exc = None
            break
        except Exception as exc:  # noqa: BLE001 — retried once, then surfaced below
            persist_exc = exc
            logger.warning(
                "action_gate: record_spend attempt %d/2 failed (tier=%s): %s",
                attempt + 1, tier, exc,
            )

    budget_persisted = persist_exc is None
    if not budget_persisted:
        logger.error(
            "action_gate: record_spend failed twice — spend already happened "
            "(tier=%s) but budget.json was NOT updated; the monthly cap will "
            "under-count until the next daily reset: %s",
            tier, persist_exc,
        )

    cost = float(new_budget.get("spent", spent_before)) - spent_before

    result = {
        "ok": True,
        "response": response_text,
        "cost": cost,
        "model": model_id,
        "reason": None if budget_persisted else f"budget_not_persisted:{persist_exc}",
        "tier": tier,
        "caller": caller,
        "budget_persisted": budget_persisted,
    }
    _idempotency_store(intent_d, result)
    return result


__all__ = [
    "escalate_for_action", "screen_intent", "ActionIntent", "IntentKind",
    "TIER_MODELS",
]
