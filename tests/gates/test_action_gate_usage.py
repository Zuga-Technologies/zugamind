"""action_gate: real usage-based cost from the Claude client, and empty
answers are failures (2026-08-28 models/ audit)."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import foundation.budget as budget
import gates.action_gate as action_gate
from foundation.config import HAIKU_COST


@pytest.fixture()
def real_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(budget, "BUDGET_FILE", tmp_path / "budget.json")
    monkeypatch.setattr(budget, "ENGINE_DIR", tmp_path)
    monkeypatch.setattr(action_gate, "_resolve_budget_helpers",
                        lambda: (budget.can_spend, budget.record_spend, budget.load_budget))
    monkeypatch.setattr(action_gate, "_idempotency_cache", {})
    return tmp_path / "budget.json"


def _intent(summary):
    return {"kind": "decide", "summary": summary, "tier": "haiku", "caller": "test"}


def test_real_cost_from_usage_is_what_gets_recorded(real_budget):
    def fake_claude():
        def _api(prompt, model, max_tokens=500, system="", usage_out=None):
            if usage_out is not None:
                usage_out.update({"usage": {"input_tokens": 1000, "output_tokens": 100}, "cost_usd": 0.0015,
                                  "model": model, "stop_reason": "end_turn"})
            return "ship it"
        return _api
    with patch.object(action_gate, "_resolve_claude_caller", fake_claude):
        r = action_gate.escalate_for_action(_intent("real cost"))
    assert r["ok"] is True
    assert r["cost"] == pytest.approx(0.0015)
    assert r["usage"] == {"input_tokens": 1000, "output_tokens": 100}
    assert json.loads(real_budget.read_text())["spent"] == pytest.approx(0.0015)


def test_without_usage_the_flat_estimate_is_recorded(real_budget):
    def fake_claude():
        return lambda *a, **kw: "ship it"
    with patch.object(action_gate, "_resolve_claude_caller", fake_claude):
        r = action_gate.escalate_for_action(_intent("flat cost"))
    assert r["ok"] is True
    assert r["cost"] == pytest.approx(HAIKU_COST)


def test_empty_answer_is_a_failed_call_not_a_decision(real_budget):
    def fake_claude():
        return lambda *a, **kw: "   "
    with patch.object(action_gate, "_resolve_claude_caller", fake_claude):
        r = action_gate.escalate_for_action(_intent("empty"))
    assert r["ok"] is False and r["reason"] == "api_error"
    assert not real_budget.exists() or json.loads(real_budget.read_text()).get("spent", 0.0) == 0.0
