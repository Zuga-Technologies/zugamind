"""Tests for foundation/budget.py — the hard $ cap gate.

Covers: local tier always affordable regardless of remaining budget, paid
tiers gated on `remaining`, spend/persist round-tripping, and the monthly
rollover semantics — the cap is per calendar MONTH, so a ledger must carry
across day boundaries (regression: an earlier version reset on every new
day, silently turning $N/month into $N/day).
"""
import json
from datetime import date

import foundation.budget as budget
import foundation.config as config


def test_local_tier_always_affordable_even_at_zero_remaining():
    assert budget.can_spend({"remaining": 0.0}, "local") is True
    assert budget.can_spend({"remaining": -1.0}, "local") is True


def test_paid_tier_blocked_when_insufficient_remaining():
    assert budget.can_spend({"remaining": 0.001}, "haiku") is False
    assert budget.can_spend({"remaining": 0.001}, "sonnet") is False


def test_paid_tier_allowed_when_sufficient_remaining():
    assert budget.can_spend({"remaining": 1.0}, "haiku") is True
    assert budget.can_spend({"remaining": 1.0}, "sonnet") is True


def test_record_spend_deducts_and_bumps_call_counter():
    b = {"spent": 0.0, "remaining": config.monthly_cap(),
         "calls": {"local": 0, "haiku": 0, "sonnet": 0, "opus": 0}, "paid_spent": 0.0}
    updated = budget.record_spend(b, "haiku")
    assert updated["calls"]["haiku"] == 1
    assert updated["spent"] == config.HAIKU_COST
    assert updated["remaining"] == round(config.monthly_cap() - config.HAIKU_COST, 4)


def test_record_spend_local_tier_costs_nothing():
    b = {"spent": 0.0, "remaining": config.monthly_cap(),
         "calls": {"local": 0, "haiku": 0, "sonnet": 0, "opus": 0}, "paid_spent": 0.0}
    updated = budget.record_spend(b, "local")
    assert updated["calls"]["local"] == 1
    assert updated["spent"] == 0.0


def test_load_budget_round_trips_through_disk(tmp_path, monkeypatch):
    budget_file = tmp_path / "budget.json"
    monkeypatch.setattr(budget, "BUDGET_FILE", budget_file)
    monkeypatch.setattr(budget, "ENGINE_DIR", tmp_path)

    b = budget.load_budget()
    b = budget.record_spend(b, "haiku")
    assert budget_file.exists()

    on_disk = json.loads(budget_file.read_text())
    assert on_disk["calls"]["haiku"] == 1


def test_load_budget_carries_spend_within_the_month(tmp_path, monkeypatch):
    """A same-month ledger is returned as-is: `remaining` must NOT refill
    just because the day changed — the cap is monthly."""
    budget_file = tmp_path / "budget.json"
    monkeypatch.setattr(budget, "BUDGET_FILE", budget_file)
    monkeypatch.setattr(budget, "ENGINE_DIR", tmp_path)

    month = date.today().strftime("%Y-%m")
    budget_file.write_text(json.dumps({
        "month": month, "spent": 9.75, "paid_spent": 9.75,
        "calls": {"local": 0, "haiku": 0, "sonnet": 195, "opus": 0},
        "remaining": 0.25,
    }))

    b = budget.load_budget()
    assert b["spent"] == 9.75
    assert b["remaining"] == 0.25
    assert budget.can_spend(b, "sonnet") is True   # 0.25 left covers one sonnet call
    assert budget.can_spend(b, "opus") is False    # but not an opus call ($0.50)


def test_load_budget_resets_on_new_month(tmp_path, monkeypatch):
    budget_file = tmp_path / "budget.json"
    monkeypatch.setattr(budget, "BUDGET_FILE", budget_file)
    monkeypatch.setattr(budget, "ENGINE_DIR", tmp_path)

    budget_file.write_text(json.dumps({
        "month": "2020-01", "spent": 9.5, "paid_spent": 9.5,
        "calls": {"local": 0, "haiku": 0, "sonnet": 190, "opus": 0},
        "remaining": 0.5,
    }))

    b = budget.load_budget()
    assert b["month"] == date.today().strftime("%Y-%m")
    assert b["spent"] == 0.0
    assert b["remaining"] == round(config.monthly_cap(), 4)


def test_load_budget_adopts_legacy_daily_ledger_from_same_month(tmp_path, monkeypatch):
    """Ledgers written by the old daily-keyed version carry a "date" key.
    Same-month spend is adopted, not discarded."""
    budget_file = tmp_path / "budget.json"
    monkeypatch.setattr(budget, "BUDGET_FILE", budget_file)
    monkeypatch.setattr(budget, "ENGINE_DIR", tmp_path)

    today = str(date.today())
    budget_file.write_text(json.dumps({
        "date": today, "spent": 3.0,
        "calls": {"local": 0, "haiku": 0, "sonnet": 60, "opus": 0},
        "remaining": 7.0,
    }))

    b = budget.load_budget()
    assert b["month"] == today[:7]
    assert "date" not in b
    assert b["spent"] == 3.0
    assert b["paid_spent"] == 0.0  # legacy field backfilled
