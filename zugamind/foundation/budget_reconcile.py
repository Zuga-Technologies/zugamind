"""Fold spends that were BILLED but never recorded back into the ledger.

The gap this closes, in one sentence: a paid call can succeed at the provider
and then fail to write to budget.json, and every later call reloads the file
fresh -- so one failed write silently under-counts the monthly cap for the
REST OF THE MONTH (the ledger is month-keyed; there is no daily reset).

`gates/action_gate.py` already handles that correctly at the moment it
happens: it keeps the paid-for answer, retries the write once, logs loudly,
and journals a structured `budget_persist_failed` event carrying the tier,
the caller and the estimated cost. Its own comment says the intended repair
-- "sum the budget_persist_failed events, fold into spent" -- and until now
nothing did it. That is this module.

Deliberately NOT the provider's usage API, which is a separate concern and
now a separate file. The journal is already on disk, already structured,
already written at the only moment the information exists, so reconciling
from it needs no credential and no network -- which is why this is the
DEFAULT repair and runs anywhere.

`foundation/cost_report.py` is the cross-check that sits beside this one,
not instead of it. It asks Anthropic's /v1/organizations/cost_report what
was actually billed. It is strictly more authoritative and strictly less
available: it needs an Admin credential (an ordinary API key is refused) and
a network round trip. The two catch DIFFERENT failures -- this file repairs
spends we know we made and failed to write down; that one catches a per-call
cost ESTIMATE that is simply wrong, which drifts the ledger a little on
every single call and leaves no trace on disk for this file to find.

Idempotent: every folded event is marked in a companion set, so running
reconcile twice does not double-count. Stdlib only.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from continuity import journal
from foundation.budget import load_budget, save_budget
from foundation.config import ENGINE_DIR
from foundation.fs import atomic_write_text

logger = logging.getLogger("zugamind.budget_reconcile")

# Events already folded in, so a second run is a no-op. Keyed by the event's
# own timestamp plus caller, which is what makes this safe to run from cron.
_APPLIED_FILE = "budget_reconciled.json"


def _applied_path():
    return ENGINE_DIR / _APPLIED_FILE


def _load_applied() -> set:
    try:
        data = json.loads(_applied_path().read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except Exception:  # noqa: BLE001 — a missing or bad marker means "none yet"
        return set()


def _save_applied(keys: set) -> None:
    try:
        atomic_write_text(_applied_path(), json.dumps(sorted(keys)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("reconcile: could not persist the applied set (%s) — "
                       "the next run may double-count; fix this before "
                       "re-running", exc)


def _event_key(event: dict) -> str:
    return f"{event.get('ts')}|{event.get('caller')}|{event.get('tier')}"


def find_unrecorded(limit: int = 5000) -> list:
    """Every budget_persist_failed event not yet folded into the ledger."""
    applied = _load_applied()
    out = []
    try:
        events = journal.read_events(limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("reconcile: journal unreadable (%s)", exc)
        return []
    for event in events:
        if event.get("kind") != "budget_persist_failed":
            continue
        if _event_key(event) in applied:
            continue
        out.append(event)
    return out


def reconcile(*, dry_run: bool = False, limit: int = 5000) -> dict[str, Any]:
    """Fold unrecorded spends into budget.json. Returns a summary.

    `dry_run` reports what WOULD be folded and touches nothing -- which is
    how you should look at it first, because this moves a money number.
    """
    pending = find_unrecorded(limit=limit)
    total = 0.0
    for event in pending:
        cost = event.get("estimated_cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost > 0:
            total += float(cost)

    summary = {
        "events": len(pending),
        "amount": round(total, 6),
        "dry_run": dry_run,
        "by_caller": {},
    }
    for event in pending:
        cost = event.get("estimated_cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            name = str(event.get("caller") or "unattributed")
            summary["by_caller"][name] = round(
                summary["by_caller"].get(name, 0.0) + float(cost), 6)

    if dry_run or not pending or total <= 0:
        return summary

    try:
        budget = load_budget()
        budget["spent"] = float(budget.get("spent", 0.0)) + total
        budget["paid_spent"] = float(budget.get("paid_spent", 0.0)) + total
        from foundation.config import monthly_cap  # noqa: WPS433 — lazy, like budget.py
        budget["remaining"] = round(monthly_cap() - budget["spent"], 4)
        save_budget(budget)
    except Exception as exc:  # noqa: BLE001 — never raise into a cron caller
        logger.warning("reconcile: could not update the ledger (%s)", exc)
        summary["error"] = str(exc)[:200]
        return summary

    applied = _load_applied()
    applied.update(_event_key(e) for e in pending)
    _save_applied(applied)

    journal.append_event("budget_reconciled", {
        "events": len(pending), "amount": round(total, 6),
        "by_caller": summary["by_caller"],
    })
    summary["applied"] = True
    return summary


__all__ = ["reconcile", "find_unrecorded"]
