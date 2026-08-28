"""foundation/fs.py — the engine's one atomic writer, and the rule that every
engine state write goes through it.

The second test is the enforcement half of a rule that used to be a comment
in four files: it fails the moment someone adds a plain `write_text(json...)`
state write anywhere under zugamind/ (audit 2026-08-28 found eight).
"""
from __future__ import annotations

import re
from pathlib import Path

from foundation import fs

ENGINE = Path(__file__).resolve().parent.parent.parent / "zugamind"


def test_write_then_read_round_trips(tmp_path):
    p = tmp_path / "deep" / "state.json"
    fs.atomic_write_text(p, '{"a": 1}')
    assert p.read_text(encoding="utf-8") == '{"a": 1}'
    assert [q.name for q in p.parent.iterdir()] == ["state.json"]  # no temp litter


def test_crash_before_replace_keeps_the_old_file(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    fs.atomic_write_text(p, "old")

    def boom(src, dst):
        raise OSError("simulated crash")
    monkeypatch.setattr(fs.os, "replace", boom)
    try:
        fs.atomic_write_text(p, "new")
    except OSError:
        pass
    assert p.read_text(encoding="utf-8") == "old"
    assert [q.name for q in tmp_path.iterdir()] == ["state.json"]


def test_no_engine_module_writes_json_state_without_the_atomic_writer():
    """Every `write_text(json.dumps(...))` under zugamind/ is a torn-file bug
    waiting for a kill signal. Allowed exceptions are listed explicitly."""
    plain = re.compile(r"\.write_text\(\s*_?json\.dumps")
    offenders = []
    for path in ENGINE.rglob("*.py"):
        if "tools" in path.parts:  # one-off operator tools, not engine state
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if plain.search(line):
                offenders.append(f"{path.relative_to(ENGINE)}:{lineno}")
    assert offenders == [], f"plain JSON state writes (use foundation.fs.atomic_write_text): {offenders}"
