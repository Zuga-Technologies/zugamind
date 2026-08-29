"""The identity loader gets its caller: every model call through the action
gate carries the facet's identity as the head of its system prompt --
SENTINEL on the local tier, DELIBERATIVE on the paid tiers. Ships DARK
behind ZUGAMIND_IDENTITY_PROMPT_ENABLED because it changes every live
prompt; off, the system prompt is byte-identical to the intent's own."""
from __future__ import annotations

from unittest.mock import patch

import pytest

import foundation.identity as identity
import gates.action_gate as action_gate


def _budget(remaining: float = 5.0) -> dict:
    return {"date": "2026-01-01", "spent": 0.0, "paid_spent": 0.0,
            "calls": {"local": 0, "haiku": 0, "sonnet": 0, "opus": 0},
            "remaining": remaining}


def _record_spend(budget, tier):
    budget["calls"][tier] = budget["calls"].get(tier, 0) + 1
    return budget


def _helpers_ok():
    return (lambda _b, _t: True), _record_spend, lambda: _budget()


@pytest.fixture(autouse=True)
def _fresh():
    action_gate._idempotency_cache.clear()


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("ZUGAMIND_IDENTITY_PROMPT_ENABLED", "true")


def _anchors() -> str:
    return (identity.PERSONA_DIR / "identity_anchors.md").read_text(encoding="utf-8").strip()


def _local_call(intent_system: str) -> str:
    seen = {}

    def fake_ollama(prompt, max_tokens=500, system="", **kw):
        seen["system"] = system
        return "ok"

    with patch.object(action_gate, "_resolve_budget_helpers", _helpers_ok), \
         patch.object(action_gate, "_resolve_ollama_caller", lambda: fake_ollama):
        r = action_gate.escalate_for_action(
            {"kind": "other", "summary": "x", "tier": "local", "caller": "t.local",
             "system": intent_system})
    assert r["ok"], r
    return seen["system"]


def _paid_call(intent_system: str) -> str:
    seen = {}

    def fake_claude():
        def _api(prompt, model, max_tokens=500, system="", **kw):
            seen["system"] = system
            return "ok"
        return _api

    with patch.object(action_gate, "_resolve_budget_helpers", _helpers_ok), \
         patch.object(action_gate, "_resolve_claude_caller", fake_claude):
        r = action_gate.escalate_for_action(
            {"kind": "other", "summary": "y", "tier": "haiku", "caller": "t.paid",
             "system": intent_system})
    assert r["ok"], r
    return seen["system"]


def test_local_tier_speaks_as_the_sentinel(on):
    charter = (identity.PERSONA_DIR / "charter.md").read_text(encoding="utf-8").strip()
    system = _local_call("BE BRIEF")
    assert system.startswith(_anchors()[:40])
    assert charter[:40] not in system  # sentinel core is anchors only; no charter body
    assert system.endswith("BE BRIEF")


def test_paid_tier_speaks_as_the_deliberative_self(on):
    charter = (identity.PERSONA_DIR / "charter.md").read_text(encoding="utf-8").strip()
    system = _paid_call("BE BRIEF")
    assert system.startswith(_anchors()[:40])
    assert charter[:40] in system
    assert system.endswith("BE BRIEF")


def test_the_runtime_override_reaches_the_prompt(on):
    path = identity.override_path("sentinel")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Check the disk before the network.", encoding="utf-8")

    system = _local_call("BE BRIEF")

    assert "Check the disk before the network." in system
    assert system.index("Check the disk") < system.index("BE BRIEF")


def test_dark_by_default_leaves_the_system_prompt_byte_identical(monkeypatch):
    monkeypatch.delenv("ZUGAMIND_IDENTITY_PROMPT_ENABLED", raising=False)
    assert _local_call("BE BRIEF") == "BE BRIEF"
    assert _paid_call("") == ""


def test_a_broken_identity_loader_never_blocks_a_call(on, monkeypatch):
    def _boom(_facet):
        raise RuntimeError("persona dir unreadable")
    monkeypatch.setattr(identity, "get_system_prompt", _boom)
    assert _local_call("BE BRIEF") == "BE BRIEF"
