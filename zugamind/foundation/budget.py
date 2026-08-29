"""ZugaMind budget tracking — load/save, can_spend gate, record_spend.

This is the hard $ cap gate for every paid-tier model call. Any code path
that calls a paid model tier (haiku/sonnet/opus) MUST call `can_spend()`
before the call and `record_spend()` after it succeeds. The local tier is
always free and always affordable.

Fully self-contained: a JSON file (`config.BUDGET_FILE`) plus
`config.monthly_cap()`. No external services, no shared fleet-wide budget
manager — see `foundation/config.py` for why (the private origin repo read
this from a cross-repo service that isn't part of this OSS release).
"""

import json
import logging
import math
from datetime import date

from foundation.config import BUDGET_FILE, ENGINE_DIR, HAIKU_COST, SONNET_COST, OPUS_COST, monthly_cap
from foundation.fs import atomic_write_text

logger = logging.getLogger("zugamind.budget")

FREE_TIER = "local"
_COSTS = {FREE_TIER: 0.0, "haiku": HAIKU_COST, "sonnet": SONNET_COST,
          "opus": OPUS_COST}


def load_budget() -> dict:
    """Load this month's budget ledger, resetting counters on a new MONTH.

    Shape: {month, spent, paid_spent, calls: {local, haiku, sonnet, opus}, remaining}.

    The ledger must carry across day boundaries within a calendar month:
    the cap is monthly, so a new day must NOT refill `remaining`. (An
    earlier version keyed the ledger on the calendar day, which silently
    turned the advertised $N/month ceiling into $N/day.) Ledgers written
    by that older version carry a "date" key instead of "month" — their
    spend is adopted into the current month, not discarded.
    """
    month = date.today().strftime("%Y-%m")
    if BUDGET_FILE.exists():
        budget = json.loads(BUDGET_FILE.read_text(encoding="utf-8"))
        if not isinstance(budget, dict):
            raise ValueError(f"budget ledger is {type(budget).__name__}, not an object")
        ledger_month = budget.get("month") or str(budget.get("date", ""))[:7]
        if ledger_month == month:
            budget["month"] = month
            budget.pop("date", None)
            return _normalised(budget)

    # New month (or first boot) — fresh counters against the full monthly cap.
    return {
        "month": month,
        "spent": 0.0,
        "paid_spent": 0.0,
        "calls": {"local": 0, "haiku": 0, "sonnet": 0, "opus": 0},
        "spent_by_caller": {},
        "remaining": round(monthly_cap(), 4),
    }


# Cap on distinct callers tracked. A ledger that grows one key per caller
# forever is the unbounded-growth shape this repo has fixed twice already;
# the point is attribution, and a caller too small to make the top N is not
# the one exhausting your cap.
_MAX_CALLERS = 32


def _attribute(existing, caller: "str | None", cost: float) -> dict:
    """Add `cost` to `caller`'s running total. Bounded, never raises."""
    out = dict(existing) if isinstance(existing, dict) else {}
    name = (caller or "unattributed").strip()[:64] or "unattributed"
    try:
        out[name] = round(float(out.get(name, 0.0) or 0.0) + cost, 6)
    except (TypeError, ValueError):
        out[name] = round(cost, 6)
    if len(out) > _MAX_CALLERS:
        # Keep the biggest spenders; fold the tail into one row rather than
        # dropping it, so the per-caller totals still sum to `spent`.
        ranked = sorted(out.items(), key=lambda kv: kv[1], reverse=True)
        kept = dict(ranked[:_MAX_CALLERS - 1])
        kept["other"] = round(sum(v for _, v in ranked[_MAX_CALLERS - 1:]), 6)
        out = kept
    return out


def _money(value, default: float, field: str) -> float:
    """Coerce a ledger money field, erring EXPENSIVE.

    A field that cannot be read is not evidence that nothing was spent. The
    old code trusted whatever was on disk, so `spent: null` or `spent: "0"`
    read as zero-spent and re-granted the whole month.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        logger.warning("budget ledger field %r is %r — treating the month as "
                       "fully spent rather than assuming it is free",
                       field, value)
        return default
    if not math.isfinite(value) or value < 0:
        logger.warning("budget ledger field %r is %r — treating the month as "
                       "fully spent", field, value)
        return default
    return float(value)


def _normalised(budget: dict) -> dict:
    """Make a loaded ledger safe to gate on, whatever is actually in the file.

    Every failure here resolves toward SPENDING LESS, never more. A ledger
    that is one key short used to pass can_spend() and then raise inside
    record_spend(), so the write never landed, the file never changed, and
    the next call reloaded the identical bad ledger and approved again --
    an unbounded-spend loop with ok:True on every call and nothing in the
    ledger to show for it (measured 2026-08-29: 40 approved calls, $16.80
    billed against a $10 cap, ledger still reading spent 0.0).
    """
    cap = monthly_cap()
    # An unreadable `spent` becomes the full cap: refuse to spend rather than
    # re-grant a month whose real spend we cannot determine.
    spent = _money(budget.get("spent"), cap, "spent")
    budget["spent"] = spent
    budget["paid_spent"] = _money(budget.get("paid_spent", 0.0), spent, "paid_spent")

    calls = budget.get("calls")
    if not isinstance(calls, dict):
        if calls is not None:
            logger.warning("budget ledger 'calls' is %r — rebuilding it", calls)
        calls = {}
    for tier in _COSTS:
        raw = calls.get(tier, 0)
        calls[tier] = raw if isinstance(raw, int) and not isinstance(raw, bool) else 0
    budget["calls"] = calls

    # `remaining` is what can_spend actually gates on, so a stale or edited
    # value is a direct grant. Clamp it to what the recorded spend allows: it
    # may be smaller than the file claims, never larger.
    on_disk = budget.get("remaining")
    derived = round(cap - spent, 4)
    if isinstance(on_disk, (int, float)) and not isinstance(on_disk, bool) \
            and math.isfinite(on_disk):
        budget["remaining"] = min(float(on_disk), derived)
    else:
        budget["remaining"] = derived
    return budget


def save_budget(budget: dict) -> None:
    """Persist budget state."""
    ENGINE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(BUDGET_FILE, json.dumps(budget, indent=2))


def can_spend(budget: dict, tier: str) -> bool:
    """Check if we can afford a call at this tier.

    The "local" tier ($0, free Ollama) is always affordable and is never
    gated on `remaining` — a paid-tier gate must never freeze the free tier.
    Paid tiers check that the estimated cost fits within what's left of the
    monthly cap.
    """
    if tier == FREE_TIER:
        return True
    if tier not in _COSTS:
        # Unknown tier used to fall through _COSTS.get(tier, 0.0) -> cost 0 ->
        # "free" -> approved at $0.00 remaining. A tier this ledger cannot
        # price is a tier it cannot gate, and the safe answer to "may I spend
        # an unknown amount" is no (audit 2026-08-29).
        logger.warning("budget: refusing unpriced tier %r", tier)
        return False
    cost = _COSTS[tier]
    if cost <= 0:
        # A PAID tier configured to cost nothing disables the cap for the
        # most expensive model in the table. Treat it as misconfiguration.
        logger.warning("budget: paid tier %r is priced at %s — refusing "
                       "rather than treating it as free", tier, cost)
        return False
    remaining = budget.get("remaining", 0.0)
    if not isinstance(remaining, (int, float)) or isinstance(remaining, bool) \
            or not math.isfinite(remaining):
        return False
    return remaining >= cost


def record_spend(budget: dict, tier: str, cost: "float | None" = None,
                 caller: "str | None" = None) -> dict:
    """Record a spend event: deduct cost, bump the call counter, persist.

    `cost` is the real USD amount from the provider's usage block when the
    caller has it (cognition.models.claude fills it from the response); the
    flat per-tier estimate — the number can_spend() gated on BEFORE the call,
    when nothing was known — is used otherwise. Before 2026-08-28 every call
    was charged the estimate, so a cache hit cost the same as a cache miss
    and a one-word answer the same as a max_tokens one.

    Local ($0) calls only bump the call counter (no disk write required —
    callers may still choose to persist at a cycle boundary). Paid-tier
    spends are written to disk immediately for crash-durability.
    """
    # `caller` is attributed, not just logged. It was threaded through every
    # spend in action_gate and recorded nowhere, so one caller could exhaust
    # the shared monthly cap and nothing afterwards could say which one
    # (audit 2026-08-29). The ledger already keeps per-tier counts; this is
    # the same question asked of the other axis.
    cost = float(cost) if cost is not None else _COSTS.get(tier, 0.0)
    if not math.isfinite(cost) or cost < 0:
        logger.warning("budget: ignoring nonsensical cost %r for tier %r", cost, tier)
        cost = _COSTS.get(tier, 0.0)

    if cost > 0:
        # Re-read the ledger and apply the DELTA to what is on disk now, not
        # to the snapshot this caller loaded before its (multi-second) model
        # call. Between those two moments another process -- the daemon and a
        # hand-run CLI genuinely coexist here, see foundation/fs.py -- may
        # have recorded its own spend. Applying to the stale snapshot silently
        # discarded it: measured 2026-08-29, two processes x 50 calls of $0.01
        # recorded $0.52 of the $1.00 actually billed, so the effective cap was
        # ~2x the real one.
        try:
            current = load_budget()
        except Exception as exc:  # noqa: BLE001 — a bad file must not lose THIS spend
            logger.warning("budget: could not re-read the ledger (%s); "
                           "applying to the in-memory copy", exc)
            current = budget

        new_spent = _money(current.get("spent"), monthly_cap(), "spent") + cost
        new_calls = dict(current.get("calls") or {})
        new_calls[tier] = int(new_calls.get(tier, 0) or 0) + 1
        # Build the whole next state BEFORE writing, then write a copy. The
        # old code mutated the caller's dict first and saved second, so a
        # save that raised left the mutation behind -- and action_gate's
        # retry called this again on the already-mutated dict, charging one
        # call twice (proved: a $0.42 call recorded $0.84 and opus: 2).
        nxt = dict(current)
        nxt["spent"] = new_spent
        nxt["paid_spent"] = _money(current.get("paid_spent", 0.0), 0.0,
                                   "paid_spent") + cost
        nxt["calls"] = new_calls
        nxt["spent_by_caller"] = _attribute(current.get("spent_by_caller"),
                                            caller, cost)
        nxt["remaining"] = round(monthly_cap() - new_spent, 4)
        save_budget(nxt)
        budget.clear()
        budget.update(nxt)
        return budget

    # Free tier: counter only, no disk write required.
    calls = budget.get("calls")
    if not isinstance(calls, dict):
        calls = {}
        budget["calls"] = calls
    calls[tier] = int(calls.get(tier, 0) or 0) + 1
    budget["spent"] = _money(budget.get("spent", 0.0), 0.0, "spent")
    budget["remaining"] = round(monthly_cap() - budget["spent"], 4)
    return budget
