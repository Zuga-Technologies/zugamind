"""Tests for foundation/identity.py -- the runtime override path is resolved
at CALL time, so a test's DATA_DIR redirect (tests/conftest.py) reaches it
and a self-modification lands in the file the loader actually reads."""
from __future__ import annotations

import foundation.config as config
from foundation import identity


def test_override_path_follows_data_dir_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "a")
    assert identity.override_path("sentinel") == tmp_path / "a" / "overrides" / "sentinel.md"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "b")
    assert identity.override_path("sentinel") == tmp_path / "b" / "overrides" / "sentinel.md"


def test_a_facets_override_path_is_live_not_frozen_at_import(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "c")
    assert identity.SENTINEL.vault_override_path == identity.override_path("sentinel")
    assert identity.DELIBERATIVE.vault_override_path == identity.override_path("deliberative")


def test_system_prompt_is_shipped_persona_then_the_runtime_override():
    path = identity.override_path("deliberative")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Prefer the smaller fix.", encoding="utf-8")

    prompt = identity.get_system_prompt(identity.DELIBERATIVE)

    anchors = (identity.PERSONA_DIR / "identity_anchors.md").read_text(encoding="utf-8").strip()
    assert prompt.startswith(anchors[:40])
    assert prompt.endswith("Prefer the smaller fix.")


def test_a_missing_override_is_simply_skipped():
    assert not identity.override_path("sentinel").exists()
    prompt = identity.get_system_prompt(identity.SENTINEL)
    anchors = (identity.PERSONA_DIR / "identity_anchors.md").read_text(encoding="utf-8").strip()
    assert prompt == anchors
