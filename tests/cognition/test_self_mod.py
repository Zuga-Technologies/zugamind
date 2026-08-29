"""Tests for the cognition self-modification path (the SelfModCooldown socket).

self_mod_cooldown.SelfModCooldown existed with no caller: nothing in the
repo ever proposed changing one of the agent's own cognition files, so
there was nothing for the cooldown to cool. This is that path.

Decision 2 (Buga, 2026-08-29): REAL -- the agent may apply the change
itself, not only propose it. The target is deliberately narrow: the per-
facet override file `foundation/identity.py` already reads at runtime
(DATA_DIR/overrides/<facet>.md), addressed by facet NAME. Shipped persona,
Python source, gates, and the cooldown's own db are not reachable.
"""
from __future__ import annotations

import json

import pytest

import foundation.config as config
from cognition import self_mod
from continuity import journal
from foundation import identity
from gates.self_mod_cooldown import SelfModCooldown


def _events(kind):
    return [e for e in journal.read_events(limit=500) if e.get("kind") == kind]


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("ZUGAMIND_SELF_MOD_ENABLED", "true")


# ---------------------------------------------------------------------------
# the whole reason the cooldown class is disk-backed
# ---------------------------------------------------------------------------

def test_a_second_proposal_for_the_same_facet_inside_24h_is_refused_across_a_restart(enabled, tmp_path):
    db = tmp_path / "cooldown.db"

    first = self_mod.propose(
        "deliberative", "Prefer the smaller fix.", why="reflection said so",
        evidence="reflection@1", cooldown=SelfModCooldown(db_path=db), now=1_000.0,
    )
    assert first["applied"] is True

    # A FRESH instance on the same db is what a process restart looks like.
    second = self_mod.propose(
        "deliberative", "Prefer the bigger fix.", why="changed my mind",
        evidence="reflection@2", cooldown=SelfModCooldown(db_path=db), now=1_000.0 + 3600,
    )

    assert second["applied"] is False
    assert second["reason"] == "cooling"
    assert second["remaining_seconds"] == pytest.approx(23 * 3600)
    assert self_mod.override_path("deliberative").read_text(encoding="utf-8") == "Prefer the smaller fix."
    refused = _events("cognition_mod_refused")
    assert len(refused) == 1 and refused[0]["remaining_seconds"] == pytest.approx(23 * 3600)


# ---------------------------------------------------------------------------
# "real": the file the identity loader reads is what changes
# ---------------------------------------------------------------------------

def test_an_applied_mod_rewrites_the_override_the_identity_loader_reads(enabled, tmp_path):
    self_mod.propose("sentinel", "Watch the disk first.", why="alarms clustered",
                     evidence="alarm x3", cooldown=SelfModCooldown(db_path=tmp_path / "c.db"))

    path = self_mod.override_path("sentinel")
    facet = identity.Facet(name="sentinel", core_paths=identity.SENTINEL.core_paths,
                           vault_override_path=path, role_summary="")
    assert identity.get_system_prompt(facet).endswith("Watch the disk first.")
    applied = _events("cognition_mod_applied")
    assert len(applied) == 1 and applied[0]["facet"] == "sentinel"


def test_the_mod_targets_exactly_the_file_the_identity_loader_reads(monkeypatch):
    """identity.py resolved its paths at import, against the real DATA_DIR;
    self_mod resolves at call time (so tests stay isolated). Point them at
    the same DATA_DIR and they must agree on the file -- otherwise the
    agent would be editing a file nothing reads."""
    monkeypatch.setattr(config, "DATA_DIR", identity.OVERRIDES_DIR.parent)
    assert self_mod.override_path("deliberative") == identity.DELIBERATIVE.vault_override_path
    assert self_mod.override_path("sentinel") == identity.SENTINEL.vault_override_path


def test_the_audit_keeps_the_previous_text_so_a_human_can_revert(enabled, tmp_path):
    cool = SelfModCooldown(db_path=tmp_path / "c.db", cooldown_hours=0.0)
    self_mod.propose("deliberative", "v1", why="a", evidence="e", cooldown=cool)
    self_mod.propose("deliberative", "v2", why="b", evidence="e", cooldown=cool)

    rows = [json.loads(l) for l in self_mod.audit_path().read_text(encoding="utf-8").splitlines()]
    assert [r["after"] for r in rows] == ["v1", "v2"]
    assert rows[1]["before"] == "v1"
    assert rows[0]["before"] == ""


def test_audit_log_and_cooldown_db_live_side_by_side():
    assert self_mod.audit_path().parent == SelfModCooldown().db_path.parent


# ---------------------------------------------------------------------------
# dark-ship: off by default, the proposal is still recorded and still cools
# ---------------------------------------------------------------------------

def test_dark_by_default_records_and_cools_but_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("ZUGAMIND_SELF_MOD_ENABLED", raising=False)
    cool = SelfModCooldown(db_path=tmp_path / "c.db")

    verdict = self_mod.propose("deliberative", "Be terser.", why="w", evidence="e", cooldown=cool)

    assert verdict["applied"] is False and verdict["reason"] == "disabled"
    assert not self_mod.override_path("deliberative").exists()
    assert cool.is_cooling(str(self_mod.override_path("deliberative"))) is True
    proposed = _events("cognition_mod_proposed")
    assert len(proposed) == 1 and proposed[0]["enabled"] is False
    rows = self_mod.audit_path().read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1 and json.loads(rows[0])["applied"] is False


# ---------------------------------------------------------------------------
# the reachable set is closed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("facet", ["gates", "../identity_anchors", "action_gate.py", ""])
def test_anything_but_a_known_facet_is_refused_without_touching_the_cooldown(enabled, tmp_path, facet):
    cool = SelfModCooldown(db_path=tmp_path / "c.db")

    verdict = self_mod.propose(facet, "x", why="w", evidence="e", cooldown=cool)

    assert verdict["applied"] is False and verdict["reason"] == "unknown_facet"
    assert _events("cognition_mod_refused")[-1]["reason"] == "unknown_facet"
    assert not (config.DATA_DIR / "overrides").exists()


def test_empty_text_is_refused_a_revert_is_a_humans_rm_not_an_agents_blank(enabled, tmp_path):
    verdict = self_mod.propose("deliberative", "   ", why="w", evidence="e",
                               cooldown=SelfModCooldown(db_path=tmp_path / "c.db"))
    assert verdict["applied"] is False and verdict["reason"] == "empty_text"
