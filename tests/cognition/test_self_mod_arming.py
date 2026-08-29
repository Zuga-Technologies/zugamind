"""The human moment on the self-modification path.

Decision 2 (Buga, 2026-08-29) says the agent may APPLY a change to its own
identity, not merely propose it. That stands. What was missing is that
nothing required a human AT THE MOMENT of the change: ZUGAMIND_SELF_MOD_ENABLED
is set once and the lane is autonomous forever after.

These tests are about the second condition. A write now needs a live arming
window that a person opened and that expires on its own — the same shape as
this environment's shared-checkout hatch, which expires for exactly the
reason a permanent opt-out becomes decoration.

Every path here fails CLOSED: unarmed, expired, disarmed, unreadable and
future-stamped all mean no write.
"""
from __future__ import annotations

import json

import pytest

from cognition import self_mod
from gates.self_mod_cooldown import SelfModCooldown


@pytest.fixture
def armed_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ZUGAMIND_SELF_MOD_ENABLED", "true")
    return tmp_path / "cooldown.db"


def _propose(db, text="a standing line", facet="deliberative", now=1_000.0):
    return self_mod.propose(facet, text, why="a reason",
                            cooldown=SelfModCooldown(db_path=db), now=now)


def test_the_flag_alone_no_longer_applies_anything(armed_env):
    """The whole point: ZUGAMIND_SELF_MOD_ENABLED=true used to be the entire
    gate on an agent rewriting its own system prompt."""
    verdict = _propose(armed_env)
    assert verdict["applied"] is False
    assert verdict["reason"] == "not_armed"
    assert not self_mod.override_path("deliberative").exists()


def test_an_armed_window_lets_the_agent_apply(armed_env):
    """Decision 2 still holds — the agent applies, inside the window."""
    self_mod.arm(now=1_000.0)
    verdict = _propose(armed_env)
    assert verdict["applied"] is True and verdict["reason"] == "applied"
    assert self_mod.override_path("deliberative").read_text(
        encoding="utf-8") == "a standing line"


def test_the_window_expires_on_its_own(armed_env):
    """A window nobody has to renew is a switch with extra steps."""
    self_mod.arm(now=1_000.0)
    verdict = _propose(armed_env, now=1_000.0 + self_mod.ARM_WINDOW_SEC + 1)
    assert verdict["applied"] is False and verdict["reason"] == "not_armed"


def test_disarm_closes_it_immediately(armed_env):
    self_mod.arm(now=1_000.0)
    self_mod.disarm()
    verdict = _propose(armed_env)
    assert verdict["applied"] is False and verdict["reason"] == "not_armed"


@pytest.mark.parametrize("body,why", [
    ('{"armed_at": 9e18}', "a marker stamped in the FUTURE is a clock "
                           "artefact or a forgery, never an arming"),
    ('{"armed_at": "soon"}', "a non-numeric stamp is not an arming"),
    ("{}", "a marker with no stamp is not an arming"),
    ("not json at all", "an unreadable marker is not an arming"),
])
def test_a_marker_that_cannot_be_trusted_is_not_an_arming(armed_env, body, why):
    """Fail-closed on every error path. The whole point is that a person did
    something recently and deliberately, so anything unconfirmable is a no."""
    path = self_mod.arm_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    verdict = _propose(armed_env)
    assert verdict["applied"] is False and verdict["reason"] == "not_armed", why


def test_the_agent_cannot_arm_itself(armed_env):
    """arm() is a human action. If the reflection loop could call it, the
    window would mean nothing — so the loop must not import it."""
    import cognition.proposer as proposer
    source = __import__("inspect").getsource(proposer)
    assert "arm(" not in source and "arm_path" not in source


def test_disabled_and_unarmed_are_different_reasons(armed_env, monkeypatch):
    """They call for different actions from a human — set a flag, versus run
    one command — so they must not share a reason string."""
    self_mod.arm(now=1_000.0)
    monkeypatch.setenv("ZUGAMIND_SELF_MOD_ENABLED", "false")
    assert _propose(armed_env)["reason"] == "disabled"


def test_arming_does_not_create_the_overrides_directory(armed_env):
    """The marker is not an override. A proposal that gets refused must not
    leave an overrides/ dir behind as a side effect of arming."""
    self_mod.arm(now=1_000.0)
    assert not self_mod.override_path("deliberative").parent.exists()
