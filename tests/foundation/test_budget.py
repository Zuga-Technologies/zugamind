"""Tests for foundation/budget.py — the hard $ cap gate.

Covers: local tier always affordable regardless of remaining budget, paid
tiers gated on `remaining`, and spend/persist round-tripping.
"""
import json

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
