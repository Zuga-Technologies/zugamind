"""A spend that was billed but never recorded must find its way back.

action_gate keeps the paid-for answer when budget.json cannot be written,
retries once, and journals a structured budget_persist_failed event with the
estimated cost. Its comment says the repair is "sum the events, fold into
spent" -- and nothing did it, so one failed write under-counted the monthly
cap for the rest of the month.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

import foundation.budget as budget
import foundation.budget_reconcile as reconcile
from continuity import journal


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "engine" / "budget.json"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(budget, "BUDGET_FILE", path)
    monkeypatch.setattr(budget, "ENGINE_DIR", path.parent)
    monkeypatch.setattr(reconcile, "ENGINE_DIR", path.parent)
    path.write_text(json.dumps({
        "month": date.today().strftime("%Y-%m"), "spent": 1.0,
        "paid_spent": 1.0, "calls": {}, "remaining": 9.0,
    }))
    return path


def _failed(cost: float, caller: str = "stream.runner") -> None:
    journal.append_event("budget_persist_failed", {
        "tier": "sonnet", "estimated_cost": cost, "caller": caller,
        "error": "disk wedged",
    })


def test_an_unrecorded_spend_is_folded_back_into_the_ledger(ledger):
    _failed(0.05)
    _failed(0.05, caller="act.command_actuator")

    summary = reconcile.reconcile()

    assert summary["events"] == 2
    assert summary["amount"] == pytest.approx(0.10)
    assert json.loads(ledger.read_text())["spent"] == pytest.approx(1.10)


def test_running_it_twice_does_not_double_count(ledger):
    """This is meant to run from cron. Folding the same event twice would
    make the repair worse than the gap."""
    _failed(0.05)
    reconcile.reconcile()
    second = reconcile.reconcile()

    assert second["events"] == 0
    assert json.loads(ledger.read_text())["spent"] == pytest.approx(1.05)


def test_dry_run_reports_without_touching_the_money(ledger):
    """It moves a money number, so looking first has to be free."""
    _failed(0.05)
    summary = reconcile.reconcile(dry_run=True)

    assert summary["amount"] == pytest.approx(0.05) and summary["dry_run"] is True
    assert json.loads(ledger.read_text())["spent"] == pytest.approx(1.0)
    assert reconcile.find_unrecorded(), "a dry run must not consume the event"


def test_it_says_which_caller_owed_the_money(ledger):
    """The point of attribution: after the fact, name the caller."""
    _failed(0.05, caller="stream.runner")
    _failed(0.20, caller="stream.runner")
    _failed(0.05, caller="act.command_actuator")

    summary = reconcile.reconcile(dry_run=True)

    assert summary["by_caller"]["stream.runner"] == pytest.approx(0.25)
    assert summary["by_caller"]["act.command_actuator"] == pytest.approx(0.05)


def test_nothing_to_do_is_not_an_error(ledger):
    summary = reconcile.reconcile()
    assert summary["events"] == 0 and summary.get("error") is None
    assert json.loads(ledger.read_text())["spent"] == pytest.approx(1.0)
