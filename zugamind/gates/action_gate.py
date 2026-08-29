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
import os
import re
import time
import unicodedata
from typing import Any, Literal, TypedDict

from foundation.failure_reason import map_local_slug

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
    "opus": "claude-opus-5",
}

# Idempotency cache: prevents double-spend if a caller retries the same
# intent within a short window (e.g. a caller-side retry-on-timeout).
_IDEMPOTENCY_WINDOW_S = 5.0
# Hard cap on the cache. An entry was only ever evicted when the SAME hash was
# looked up again after expiry, so a long-lived daemon that never repeats an
# intent grew this dict forever (audit 2026-08-29). Every store now prunes what
# has expired and, if that is still not enough, drops the oldest.
_IDEMPOTENCY_MAX_ENTRIES = 512
_idempotency_cache: dict[str, tuple[float, dict]] = {}

# Circuit breaker. Nothing stopped a caller re-attempting a live model call
# every cycle through a provider outage, and a paid call that fails after the
# provider answered (a refusal, an empty content block) still costs money -- so
# a sustained outage burned budget with no chance of a usable answer (audit
# 2026-08-29). After this many CONSECUTIVE provider failures from one caller,
# its circuit opens for the cooldown and further intents are refused without a
# call. One success closes it.
_CIRCUIT_FAIL_THRESHOLD = int(os.environ.get("ZUGAMIND_ACTION_CIRCUIT_FAILS", "5"))
_CIRCUIT_COOLDOWN_S = float(os.environ.get("ZUGAMIND_ACTION_CIRCUIT_COOLDOWN_S", "300"))
_circuit_failures: dict[str, int] = {}
_circuit_open_until: dict[str, float] = {}

# Hard ceiling on one call's max_tokens. can_spend() gates on a FLAT per-tier
# estimate computed BEFORE the call, when nothing is known about output length
# -- so an intent asking for 100k tokens is approved against the cost of a
# 500-token one. This clamp is what keeps that estimate meaningful; without it
# the budget check is advisory (audit 2026-08-29).
_MAX_TOKENS_CEILING = int(os.environ.get("ZUGAMIND_ACTION_MAX_TOKENS", "4000"))


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
    "budget.py", "action_gate.py", "gates/", "gates\\", "charter.md",
)
_SHIELD_MUTATE_VERBS = ("edit", "modify", "disable", "remove", "weaken",
                        "bypass", "rewrite", "delete", "patch")

# Characters that render as nothing and split a word in two, defeating every
# pattern above: a zero-width space inside `rm -rf` is not `rm -rf` to a
# regex, but is once the text round-trips through a shell. The ranges are the
# ones with measured attack-success rates against guardrail filters of exactly
# this shape (arXiv 2504.11168): Unicode Tag block ~90%, bidirectional
# overrides ~79-99%, emoji variation-selector smuggling ~100%.
_INVISIBLE_RANGES = (
    (0x200B, 0x200F),    # zero-width space/non-joiner/joiner, LRM/RLM
    (0x202A, 0x202E),    # bidi embedding + override
    (0x2060, 0x2064),    # word joiner, invisible operators
    (0x2066, 0x2069),    # bidi isolates
    (0xFE00, 0xFE0F),    # variation selectors 1-16
    (0xE0000, 0xE007F),  # Unicode Tag block
    (0xE0100, 0xE01EF),  # variation selectors supplement
)
_INVISIBLE = {cp: None for lo, hi in _INVISIBLE_RANGES
              for cp in range(lo, hi + 1)}
_INVISIBLE.update(dict.fromkeys(map(ord, "\u00ad\u180e\ufeff\u061c"), None))
# Homoglyphs NFKC does NOT fold: Cyrillic/Greek letters visually identical to
# ASCII. A Cyrillic ge in `git push --force` reads normally and matched
# nothing. Only genuinely indistinguishable confusables are folded -- this is
# a screen, not a transliterator.
_CONFUSABLES = str.maketrans({
    "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p", "\u0441": "c",
    "\u0445": "x", "\u0443": "y", "\u0456": "i", "\u0455": "s", "\u0458": "j",
    "\u04bb": "h", "\u0433": "g", "\u043a": "k", "\u043c": "m", "\u0442": "t",
    "\u0432": "b", "\u043d": "h", "\u0410": "a", "\u0415": "e", "\u041e": "o",
    "\u0420": "p", "\u0421": "c", "\u0425": "x", "\u03bf": "o", "\u03b1": "a",
    "\u03b5": "e", "\u03c1": "p", "\u03bd": "v", "\u03ba": "k", "\u03c4": "t",
    "\u0392": "b", "\u039f": "o", "\u0391": "a", "\u2010": "-", "\u2011": "-",
    "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-", "\u2044": "/",
    "\u2215": "/",
})


def _normalize_for_screen(blob: str) -> str:
    """Fold a blob to the one spelling the patterns are written against.

    A deny-list screen matches literal text, so every cheap way of writing
    the same string differently is a bypass. Four are closed here, in order:
    compatibility normalization (NFKC -- fullwidth and ligature forms),
    invisible characters, homoglyphs, then whitespace runs. Casefold comes
    last so it also lowercases whatever the substitutions produced.

    What this does NOT close, stated plainly so nobody mistakes the screen for
    a boundary: the patterns are English, so the same instruction phrased in
    another language passes untouched, and no amount of normalization changes
    that. Full UTS-39 confusables folding needs a ~6.5k-entry table that is not
    in the stdlib, so only the unambiguous Latin lookalikes are folded here.
    This is an acute red-flag screen in a defence-in-depth stack (the budget
    cap, the human veto and the dry run are the other layers) -- it is not the
    thing standing between the agent and a determined adversary.
    """
    b = unicodedata.normalize("NFKC", blob or "")
    b = b.translate(_INVISIBLE).translate(_CONFUSABLES)
    return re.sub(r"\s+", " ", b).casefold()


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
    blob = _normalize_for_screen(" ".join(parts))
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


def _identity_enabled() -> bool:
    return os.environ.get(
        "ZUGAMIND_IDENTITY_PROMPT_ENABLED", "false",
    ).strip().lower() not in ("0", "false", "no", "off", "")


def _with_identity(system: str, tier: str) -> str:
    """Head the system prompt with the facet's identity: SENTINEL on the
    local tier, DELIBERATIVE on the paid ones (foundation/identity.py).

    That loader had no caller from v0.1.0 until 2026-08-29 -- the persona
    shipped, and no prompt ever carried it. Ships DARK behind
    ZUGAMIND_IDENTITY_PROMPT_ENABLED because it changes every live prompt;
    off, `system` comes back byte-identical. Fail-open: an unreadable
    persona must never be the reason a call does not happen.
    """
    if not _identity_enabled():
        return system
    try:
        import foundation.identity as identity  # noqa: WPS433 — lazy, keeps this file's import graph small
        facet = identity.SENTINEL if tier == "local" else identity.DELIBERATIVE
        head = identity.get_system_prompt(facet)
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.warning("action_gate: identity unavailable, prompt sent without it: %s", exc)
        return system
    if not head:
        return system
    return f"{head}\n\n{system}" if system else head


def _resolve_ollama_caller():
    from cognition.models.ollama import ollama_query  # noqa: WPS433
    return ollama_query


# --- Helpers -----------------------------------------------------------------

def _intent_hash(intent: dict) -> str:
    # `caller` is part of the key: an idempotency key scoped to the payload
    # alone lets two DIFFERENT callers issuing textually identical intents
    # inside the window share one cached response, so the second silently
    # receives an answer produced for the first. The standard rule is to scope
    # the key by tenant, never the key on its own (Stripe's idempotency
    # design); `caller` is this gate's tenant. Added 2026-08-29.
    keys = ("kind", "summary", "context", "max_tokens", "system", "tier",
            "caller")
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
    now = time.monotonic()
    _idempotency_cache[_intent_hash(intent)] = (now, response)
    if len(_idempotency_cache) <= _IDEMPOTENCY_MAX_ENTRIES:
        return
    # Nothing else evicts: a lookup only drops the key it was asked about, so a
    # daemon that never repeats an intent grew this forever. Drop what has
    # expired first -- those can never be served again -- and only if that is
    # still not enough, drop oldest-first down to the cap.
    for key in [k for k, (ts, _) in _idempotency_cache.items()
                if (now - ts) >= _IDEMPOTENCY_WINDOW_S]:
        _idempotency_cache.pop(key, None)
    if len(_idempotency_cache) > _IDEMPOTENCY_MAX_ENTRIES:
        for key in sorted(_idempotency_cache, key=lambda k: _idempotency_cache[k][0])[
            : len(_idempotency_cache) - _IDEMPOTENCY_MAX_ENTRIES
        ]:
            _idempotency_cache.pop(key, None)


def _circuit_seconds_left(caller: str, *, now: float | None = None) -> float:
    """Seconds until `caller`'s circuit closes again (0.0 when it is closed)."""
    opened_until = _circuit_open_until.get(caller)
    if opened_until is None:
        return 0.0
    left = opened_until - (time.monotonic() if now is None else now)
    if left <= 0.0:
        # Expired: clear both halves so the next failure starts a fresh count
        # rather than instantly re-opening on the stale tally.
        _circuit_open_until.pop(caller, None)
        _circuit_failures.pop(caller, None)
        return 0.0
    return left


def _circuit_record_failure(caller: str, tier: str, reason: str) -> None:
    n = _circuit_failures.get(caller, 0) + 1
    _circuit_failures[caller] = n
    if n < _CIRCUIT_FAIL_THRESHOLD or caller in _circuit_open_until:
        return
    _circuit_open_until[caller] = time.monotonic() + _CIRCUIT_COOLDOWN_S
    logger.error(
        "action_gate: circuit OPEN for caller=%s after %d consecutive provider "
        "failures (last: %s) — refusing for %.0fs",
        caller, n, reason, _CIRCUIT_COOLDOWN_S,
    )
    _journal_gate_event("gate_circuit_open", {
        "caller": caller, "tier": tier, "failures": n,
        "cooldown_s": _CIRCUIT_COOLDOWN_S, "last_reason": reason,
    })


def _circuit_record_success(caller: str) -> None:
    _circuit_failures.pop(caller, None)
    _circuit_open_until.pop(caller, None)


def _journal_gate_event(kind: str, payload: dict) -> None:
    """Best-effort structured trail. The journal is an independent file with
    independent writes, so it survives a wedged budget.json -- and it must
    never raise into the gate, which is why even the import is guarded."""
    try:
        from continuity import journal  # noqa: WPS433 — lazy, like the other helpers
        journal.append_event(kind, payload)
    except Exception as exc:  # noqa: BLE001 — the trail is best-effort by contract
        logger.warning("action_gate: journaling %s failed: %s", kind, exc)


def _tier_estimate(tier: str) -> float:
    """The flat per-tier cost can_spend() gated on. 0.0 if unresolvable."""
    try:
        from foundation.config import HAIKU_COST, OPUS_COST, SONNET_COST  # noqa: WPS433
        return {"haiku": HAIKU_COST, "sonnet": SONNET_COST,
                "opus": OPUS_COST}.get(tier, 0.0)
    except Exception:  # noqa: BLE001 — an estimate is a nicety, never a blocker
        return 0.0


def _real_cost(usage_meta: dict | None) -> float | None:
    """The provider's own USD figure, when the call actually reached it."""
    cost = usage_meta.get("cost_usd") if isinstance(usage_meta, dict) else None
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        return float(cost)
    return None


def _persist_spend(record_spend, budget: dict, tier: str, usage_meta: dict,
                   caller: str) -> tuple[dict, float, "Exception | None"]:
    """Write an ALREADY-INCURRED spend to the ledger.

    Returns (new_budget, cost, persist_exc). Real money is gone at the
    provider's end before this runs, so failure here is never a reason to
    discard anything -- it is a reason to be loud. record_spend() persists the
    fact to budget.json so the NEXT call's can_spend() sees it; every call
    reloads budget.json fresh, so a single silently-failed write under-counts
    the monthly cap for the REST OF THE MONTH (the ledger is month-keyed;
    there is no daily reset), with no concurrency race required. One retry
    absorbs a transient I/O blip; past that we report loudly and leave a
    structured trail so reconciliation is mechanical: sum the
    `budget_persist_failed` events and fold them into `spent`.
    """
    spent_before = float(budget.get("spent", 0.0))
    new_budget = budget
    persist_exc: "Exception | None" = None
    for attempt in range(2):
        try:
            real = _real_cost(usage_meta)
            if real is not None:
                new_budget = record_spend(budget, tier, cost=real)
            else:
                new_budget = record_spend(budget, tier)  # flat per-tier estimate
            persist_exc = None
            break
        except Exception as exc:  # noqa: BLE001 — retried once, then surfaced below
            persist_exc = exc
            logger.warning(
                "action_gate: record_spend attempt %d/2 failed (tier=%s): %s",
                attempt + 1, tier, exc,
            )

    if persist_exc is None:
        cost = float(new_budget.get("spent", spent_before)) - spent_before
        return new_budget, cost, None

    logger.error(
        "action_gate: record_spend failed twice — spend already happened "
        "(tier=%s) but budget.json was NOT updated; the monthly cap will "
        "under-count for the REST OF THE MONTH — reconcile budget.json "
        "manually or accept the drift: %s",
        tier, persist_exc,
    )
    estimated = _tier_estimate(tier)
    try:
        from foundation.failure_reason import normalize  # noqa: WPS433
        # Unlike the ok:True `budget_not_persisted:<exc>` reason on the returned
        # result (excluded — success rows stay NULL), this JOURNAL event IS
        # failure-shaped: a write genuinely failed. Direct literal mapping (not
        # map_local_slug's local-vocabulary table) per Buga's ruling.
        failure_reason = normalize(
            f"infrastructure: budget persist failed: {persist_exc}")
    except Exception:  # noqa: BLE001
        failure_reason = None
    _journal_gate_event("budget_persist_failed", {
        "tier": tier,
        "estimated_cost": estimated,
        "caller": caller,
        "error": str(persist_exc),
        "failure_reason": failure_reason,
    })
    # The money left the account even though the ledger did not record it.
    # Reporting 0.0 here made a failed persist look FREE to every caller that
    # sums `cost` — report the best figure available instead (audit 2026-08-29).
    real = _real_cost(usage_meta)
    return new_budget, (real if real is not None else estimated), persist_exc


# Hard ceiling on the serialized context that rides into a paid model call.
# The winner's triggers were dumped verbatim AND re-embedded by the plan step
# that copies the winner's context: five 5 KB triggers = a 51,939-char prompt
# (audit 2026-08-28). The briefing that goes to the harness has had a cap for
# months; this is the same idea for the gate's own prompt.
_PROMPT_CONTEXT_CHARS = 12_000


def _compact_context(context: dict) -> dict:
    from foundation.text_format import compact_payload  # noqa: WPS433 — lazy, like the other helpers
    compacted = compact_payload(context)
    # Every plan template copies `triggers` out of the winner's own context,
    # so a plan step's triggers are the winner's triggers, verbatim, again.
    # The model sees them once, on the winner.
    plan = compacted.get("plan")
    if isinstance(plan, list):
        for step in plan:
            if isinstance(step, dict) and isinstance(step.get("context"), dict):
                step["context"].pop("triggers", None)
    return compacted


def _build_prompt(intent: dict) -> str:
    summary = intent.get("summary", "")
    context = intent.get("context", {})
    if not context:
        return summary
    body = json.dumps(_compact_context(context) if isinstance(context, dict) else context,
                      indent=2, default=str)
    if len(body) > _PROMPT_CONTEXT_CHARS:
        body = body[:_PROMPT_CONTEXT_CHARS] + "\n… [context truncated]"
    return f"{summary}\n\nContext:\n{body}"


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
    # Write the RESOLVED caller back so _intent_hash keys on the same value the
    # result reports, rather than on a key that may not be in the intent.
    intent_d["caller"] = caller

    # `dry_run` is a call argument, not part of the intent, so it is not in
    # the hash — a dry run within the window used to be served the cached
    # response of a REAL call, reporting a paid answer and a cost it never
    # incurred (audit 2026-08-29). A dry run neither reads nor writes it.
    cached = None if dry_run else _idempotency_lookup(intent_d)
    if cached is not None:
        logger.info("action_gate: idempotent hit kind=%s caller=%s", kind, caller)
        return {**cached, "from_cache": True}

    # Human veto point: a caller can mark an intent as needing a human. The
    # gate refuses to auto-execute it -- no model call, full stop. Wiring an
    # actual notification (Discord/Slack/email) onto this is left to the
    # deployer; the OSS core just guarantees the refusal.
    if intent_d.get("requires_human"):
        _journal_gate_event("gate_refused", {
            "reason": "requires_human_review", "caller": caller, "kind": kind,
            "summary": str(intent_d.get("summary", ""))[:200],
        })
        result = {
            "ok": False,
            "response": None,
            "cost": 0.0,
            "model": "deferred",
            "reason": "requires_human_review",
            "failure_reason": map_local_slug("requires_human_review"),
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
        blocked_reason = f"shield_refused:{shield_reason}"
        # Only the SAFETY refusals are journaled, not budget/plumbing ones:
        # those are the caller's business (the runner already journals its own
        # skip) and are high-frequency once the cap is hit. A refusal to act on
        # dangerous content is the one the audit trail exists for.
        _journal_gate_event("gate_refused", {
            "reason": blocked_reason, "caller": caller, "kind": kind,
            "tier": tier, "summary": str(intent_d.get("summary", ""))[:200],
        })
        return {
            "ok": False,
            "response": None,
            "cost": 0.0,
            "model": "blocked",
            "reason": blocked_reason,
            "failure_reason": map_local_slug(blocked_reason),
            "caller": caller,
        }

    try:
        can_spend, record_spend, load_budget = _resolve_budget_helpers()
    except Exception as exc:
        logger.warning("action_gate: budget import failed: %s", exc)
        import_reason = f"import_error:{exc}"
        return {
            "ok": False,
            "response": None,
            "cost": 0.0,
            "model": "none",
            "reason": import_reason,
            "failure_reason": map_local_slug(import_reason),
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

    circuit_left = _circuit_seconds_left(caller)
    if circuit_left > 0.0:
        circuit_reason = f"circuit_open:{int(circuit_left)}s"
        logger.warning("action_gate: refusing (circuit open %.0fs, caller=%s)",
                       circuit_left, caller)
        return {
            "ok": False,
            "response": None,
            "cost": 0.0,
            "model": "none",
            "reason": circuit_reason,
            "failure_reason": map_local_slug(circuit_reason),
            "tier": tier,
            "caller": caller,
        }

    try:
        budget = load_budget()
    except Exception as exc:
        logger.warning("action_gate: load_budget failed: %s", exc)
        budget_error_reason = f"budget_error:{exc}"
        return {
            "ok": False,
            "response": None,
            "cost": 0.0,
            "model": "none",
            "reason": budget_error_reason,
            "failure_reason": map_local_slug(budget_error_reason),
            "tier": tier,
            "caller": caller,
        }

    try:
        affordable = can_spend(budget, tier)
    except Exception as exc:  # noqa: BLE001 — fail closed, matches load_budget above
        logger.warning("action_gate: can_spend check failed: %s", exc)
        can_spend_reason = f"can_spend_error:{exc}"
        return {
            "ok": False,
            "response": None,
            "cost": 0.0,
            "model": "none",
            "reason": can_spend_reason,
            "failure_reason": map_local_slug(can_spend_reason),
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
            "failure_reason": map_local_slug("budget_exhausted"),
            "tier": tier,
            "caller": caller,
        }

    usage_meta: dict = {}  # filled by the Claude client: real usage, stop reason, USD cost
    try:
        prompt = _build_prompt(intent_d)
        max_tokens = int(intent_d.get("max_tokens", 500))
        if max_tokens > _MAX_TOKENS_CEILING:
            logger.warning(
                "action_gate: max_tokens=%d exceeds the ceiling (%d) the budget "
                "check was computed against — clamping (caller=%s)",
                max_tokens, _MAX_TOKENS_CEILING, caller,
            )
            max_tokens = _MAX_TOKENS_CEILING
        system = _with_identity(str(intent_d.get("system", "")), tier)
        if tier == "local":
            ollama_query = _resolve_ollama_caller()
            response_text = ollama_query(prompt, max_tokens=max_tokens, system=system)
        else:
            query_claude_api = _resolve_claude_caller()
            response_text = query_claude_api(
                prompt, model_id, max_tokens=max_tokens, system=system, usage_out=usage_meta
            )
    except Exception as exc:
        logger.warning("action_gate: api_error kind=%s: %s", kind, exc)
        api_error_reason = f"api_error:{exc}"
        _circuit_record_failure(caller, tier, api_error_reason)
        return {
            "ok": False,
            "response": None,
            "cost": 0.0,
            "model": model_id,
            "reason": api_error_reason,
            "failure_reason": map_local_slug(api_error_reason),
            "tier": tier,
            "caller": caller,
        }

    if response_text is None or not str(response_text).strip():
        # An EMPTY answer is a failed call, not a decision with an empty
        # payload (a malformed-but-200 Ollama body used to come back as ""
        # and pass this check as ok=True — audit 2026-08-28).
        #
        # Failed for US; not free from the PROVIDER. cognition.models.claude
        # fills usage_out and THEN returns None on two real, billed outcomes:
        # a `refusal` stop_reason, and a 200 carrying no text block. Both were
        # landing here recording nothing, so an endpoint refusing in a loop was
        # invisible to the monthly cap (audit 2026-08-29). A cost in usage_meta
        # is proof the provider answered — that, and only that, is what
        # separates "billed and empty" from "never reached the provider", so
        # the ledger write is conditioned on it and an unreached call still
        # records nothing.
        _circuit_record_failure(caller, tier, "api_error:empty")
        empty_cost = 0.0
        if _real_cost(usage_meta) is not None:
            _, empty_cost, _ = _persist_spend(
                record_spend, budget, tier, usage_meta, caller)
        return {
            "ok": False,
            "response": None,
            "cost": empty_cost,
            "model": model_id,
            "reason": "api_error",
            "failure_reason": map_local_slug("api_error"),
            "tier": tier,
            "caller": caller,
        }

    _circuit_record_success(caller)
    new_budget, cost, persist_exc = _persist_spend(
        record_spend, budget, tier, usage_meta, caller)
    budget_persisted = persist_exc is None

    result = {
        "ok": True,
        "usage": usage_meta.get("usage") if isinstance(usage_meta, dict) else None,
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
