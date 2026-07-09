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
from datetime import date

from foundation.config import BUDGET_FILE, ENGINE_DIR, HAIKU_COST, SONNET_COST, OPUS_COST, monthly_cap

logger = logging.getLogger("zugamind.budget")

_COSTS = {"local": 0.0, "haiku": HAIKU_COST, "sonnet": SONNET_COST, "opus": OPUS_COST}


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
        budget = json.loads(BUDGET_FILE.read_text())
        ledger_month = budget.get("month") or str(budget.get("date", ""))[:7]
        if ledger_month == month:
            budget["month"] = month
            budget.pop("date", None)
            if "paid_spent" not in budget:
                budget["paid_spent"] = 0.0
            return budget

    # New month (or first boot) — fresh counters against the full monthly cap.
    return {
        "month": month,
        "spent": 0.0,
        "paid_spent": 0.0,
        "calls": {"local": 0, "haiku": 0, "sonnet": 0, "opus": 0},
        "remaining": round(monthly_cap(), 4),
    }


def save_budget(budget: dict) -> None:
    """Persist budget state."""
    ENGINE_DIR.mkdir(parents=True, exist_ok=True)
    BUDGET_FILE.write_text(json.dumps(budget, indent=2))


def can_spend(budget: dict, tier: str) -> bool:
    """Check if we can afford a call at this tier.

    The "local" tier ($0, free Ollama) is always affordable and is never
    gated on `remaining` — a paid-tier gate must never freeze the free tier.
    Paid tiers check that the estimated cost fits within what's left of the
    monthly cap.
    """
    cost = _COSTS.get(tier, 0.0)
    if cost == 0:
        return True
    return budget.get("remaining", 0.0) >= cost


def record_spend(budget: dict, tier: str) -> dict:
    """Record a spend event: deduct cost, bump the call counter, persist.

    Local ($0) calls only bump the call counter (no disk write required —
    callers may still choose to persist at a cycle boundary). Paid-tier
    spends are written to disk immediately for crash-durability.
    """
    cost = _COSTS.get(tier, 0.0)
    budget["spent"] = budget.get("spent", 0.0) + cost
    budget["remaining"] = round(monthly_cap() - budget["spent"], 4)
    budget["calls"][tier] = budget["calls"].get(tier, 0) + 1
    if cost > 0:
        budget["paid_spent"] = budget.get("paid_spent", 0.0) + cost
        save_budget(budget)
    return budget
