"""Tests for cognition/workspace/module_helpers.py — the extension seam.

Nothing in the shipped repo consumes these helpers (they exist for
third-party WorkspaceModule extensions — see the module docstring), so these
tests are the contract: env-gating semantics, broadcast filtering, the
never-raises guarantee, and idempotent self-registration into ALL_MODULES.
"""
from __future__ import annotations

from cognition.workspace import workspace_modules
from cognition.workspace.module_helpers import (
    emit_event,
    gate_enabled,
    make_on_broadcast,
    self_register,
)
from cognition.workspace.workspace import (
    SalienceBid,
    ThoughtType,
    WorkspaceContent,
    WorkspaceModule,
)


# --- gate_enabled: default-ON env gate --------------------------------------

def test_gate_enabled_defaults_on_when_unset(monkeypatch):
    monkeypatch.delenv("ZM_TEST_GATE", raising=False)
    assert gate_enabled("ZM_TEST_GATE") is True


def test_gate_enabled_only_literal_zero_disables(monkeypatch):
    monkeypatch.setenv("ZM_TEST_GATE", "0")
    assert gate_enabled("ZM_TEST_GATE") is False
    monkeypatch.setenv("ZM_TEST_GATE", "1")
    assert gate_enabled("ZM_TEST_GATE") is True
    monkeypatch.setenv("ZM_TEST_GATE", "false")  # not "0" — stays enabled
    assert gate_enabled("ZM_TEST_GATE") is True


# --- emit_event: never raises ------------------------------------------------

def test_emit_event_never_raises_even_on_weird_payload():
    emit_event("test_kind", {"self_ref": ...}, caller="tests")  # must not raise


# --- make_on_broadcast: fires only for own wins, swallows handler errors -----

def _content_for(module_name: str) -> WorkspaceContent:
    bid = SalienceBid(module_name, "won", 0.9, ThoughtType.KNOWLEDGE)
    return WorkspaceContent(bid=bid)


def test_make_on_broadcast_runs_only_when_own_module_won():
    calls = []
    handler = make_on_broadcast("mine", lambda bid_context: calls.append(bid_context))
    handler(None, _content_for("someone_else"))
    assert calls == []
    handler(None, _content_for("mine"))
    assert len(calls) == 1


def test_make_on_broadcast_swallows_handler_exceptions():
    def _boom(bid_context):
        raise RuntimeError("handler bug")

    handler = make_on_broadcast("mine", _boom)
    handler(None, _content_for("mine"))  # must not raise into the cycle


# --- self_register: idempotent insertion into ALL_MODULES --------------------

class _ThirdPartyModule(WorkspaceModule):
    def generate_bid(self, context):
        return None


def test_self_register_inserts_once_and_is_idempotent():
    original = list(workspace_modules.ALL_MODULES)
    try:
        self_register(_ThirdPartyModule)
        assert _ThirdPartyModule in workspace_modules.ALL_MODULES
        count_after_first = workspace_modules.ALL_MODULES.count(_ThirdPartyModule)
        self_register(_ThirdPartyModule)  # second call is a no-op
        assert workspace_modules.ALL_MODULES.count(_ThirdPartyModule) == count_after_first == 1
    finally:
        workspace_modules.ALL_MODULES[:] = original
