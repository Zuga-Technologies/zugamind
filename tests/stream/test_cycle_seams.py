"""Seams around the workspace (2026-08-28): a run_cycle exception becomes a
journal line, not a dead daemon; the gate's prompt and the per-cycle journal
event are bounded copies of the winner, not the raw payload.
"""
from __future__ import annotations

import json

import act.command_actuator as command_actuator
import continuity.journal as journal
import foundation.state as state_mod
import gates.action_gate as action_gate
from foundation.text_format import compact_payload
from stream.runner import StreamRunner


def _big_triggers(n=5, size=5_000):
    return [{"type": "local_service_down", "service": f"svc{i}", "port": i, "detail": "x" * size}
            for i in range(n)]


def _patch(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    monkeypatch.setattr(state_mod, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(state_mod, "ENGINE_DIR", tmp_path)
    monkeypatch.setattr(command_actuator, "load_quiet_hours", lambda *a, **kw: None)
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [])


# --- compact_payload -------------------------------------------------------------

def test_compact_payload_bounds_strings_lists_and_depth():
    deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": "too deep"}}}}}}}
    out = compact_payload({"s": "x" * 1000, "l": list(range(20)), **deep})
    assert len(out["s"]) == 300 and out["s"].endswith("…")
    assert len(out["l"]) == 9 and out["l"][-1] == "…+12 more"
    assert out["a"]["b"]["c"]["d"]["e"]["f"] == "…"


def test_compact_payload_never_raises_and_copies():
    class Weird:
        def __str__(self):
            raise RuntimeError("no")
    src = {"w": Weird(), "k": [1, 2]}
    out = compact_payload(src)
    assert out["w"] == "…" and out["k"] == [1, 2] and out["k"] is not src["k"]


# --- the gate's prompt is bounded ----------------------------------------------------

def test_gate_prompt_is_bounded_and_drops_the_duplicated_plan_triggers():
    winner = {"source_module": "infrastructure", "content": "CRITICAL", "salience": 0.9,
              "context": {"triggers": _big_triggers(), "n_critical": 5}}
    plan = [{"description": "restart", "action": "restart_service",
             "context": {"service": "all", "triggers": _big_triggers()}}]
    prompt = action_gate._build_prompt({"summary": "decide", "context": {"winner": winner, "plan": plan}})
    assert len(prompt) <= action_gate._PROMPT_CONTEXT_CHARS + 100
    assert "svc0" in prompt and "restart_service" in prompt   # the substance survives
    assert prompt.count("x" * 299) <= 5                      # each detail clipped once, not 10 copies


def test_small_context_is_unchanged():
    prompt = action_gate._build_prompt({"summary": "s", "context": {"a": 1}})
    assert prompt == 's\n\nContext:\n{\n  "a": 1\n}'


# --- a bad cycle is a journal line ------------------------------------------------------

def test_run_cycle_exception_is_journaled_and_the_cycle_completes(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    runner = StreamRunner(extra_scanners={"scan_toy": lambda: _big_triggers(1, 10)},
                          dry_run=True, include_default_scanners=False)

    def boom(ctx):
        raise RuntimeError("bid table on fire")
    monkeypatch.setattr(runner.workspace, "run_cycle", boom)
    result = runner.run_once()  # must not raise
    assert result["winner"] is None
    kinds = [e["kind"] for e in journal.read_events()]
    assert "cycle_error" in kinds and "cycle" in kinds
    err = next(e for e in journal.read_events() if e["kind"] == "cycle_error")
    assert "bid table on fire" in err["error"] and err["failure_reason"]
    assert (tmp_path / "state.json").exists()


# --- the journal gets a bounded winner ------------------------------------------------------

def test_cycle_event_carries_a_compacted_winner(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    runner = StreamRunner(extra_scanners={"scan_toy": lambda: _big_triggers(12)},
                          dry_run=True, include_default_scanners=False)
    result = runner.run_once()
    assert len(json.dumps(result["winner"])) > 50_000          # in-memory: the full payload
    cycle = next(e for e in journal.read_events() if e["kind"] == "cycle")
    line = json.dumps(cycle["winner"])
    assert len(line) < 6_000                                    # journal: a summary
    assert cycle["winner"]["source_module"] == "infrastructure"
