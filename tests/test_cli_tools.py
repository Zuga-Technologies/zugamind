"""cli_tools.py — doctor / logs / budget / explain / verify (2026-08-29)."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import pytest

import act.command_actuator as command_actuator
import act.floor_calibration as floor_calibration
import cli
import cognition.self_mod as _self_mod
import cli_tools
import continuity.journal as journal
import foundation.budget as budget
import foundation.config as config
import foundation.state as state_mod


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    monkeypatch.setattr(journal, "_appends_since_check", 0)
    monkeypatch.setattr(journal, "_tail_checked_for", None)
    monkeypatch.setattr(cli, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    monkeypatch.setattr(cli, "PID_FILE", tmp_path / "daemon.pid")
    monkeypatch.setattr(config, "ENGINE_DIR", tmp_path)
    monkeypatch.setattr(config, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(config, "PAUSE_FILE", tmp_path / "PAUSE")
    monkeypatch.setattr(state_mod, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(state_mod, "ENGINE_DIR", tmp_path)
    monkeypatch.setattr(budget, "BUDGET_FILE", tmp_path / "budget.json")
    monkeypatch.setattr(budget, "ENGINE_DIR", tmp_path)
    monkeypatch.setattr(floor_calibration, "STATE_FILE", tmp_path / "floor_calibration.json")
    monkeypatch.setenv("ZUGAMIND_HARNESS_CONFIG", str(tmp_path / "harness.json"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return tmp_path


def _ns(**kw):
    return argparse.Namespace(**kw)


# --- report: the agent's account of its own work, judged before it is shown --------

def test_report_shows_the_suppression_reason_not_the_draft(data_dir, monkeypatch, capsys):
    """`zugamind report` is the one place the agent composes prose about its
    own work for a human. A draft the judge refuses must come back as WHY it
    was refused -- the draft itself stays in the journal for inspection."""
    import cognition.models.ollama as ollama_mod
    import gates.work_claim as work_claim_mod

    journal.append_event("harness_invocation", {
        "harness": "claude-code", "ok": True, "dry_run": False,
        "stdout": "ClickHouse is now in our stack.",
    })
    monkeypatch.setattr(work_claim_mod, "_recent_commits", lambda *a, **kw: [])
    monkeypatch.setattr(ollama_mod, "ollama_query",
                        lambda *a, **kw: "SUPPRESS\nClickHouse appears in no commit")

    rc = cli_tools.cmd_report(_ns(minutes=60, json=False))

    out = capsys.readouterr().out
    assert rc == 1
    # entity_grounding, not judge: the noun-side check is free, deterministic
    # and now runs BEFORE the model on the human-facing path, so it catches
    # "ClickHouse is now in our stack" -- a claim with no work-claim VERB --
    # without spending an inference (audit 2026-08-29).
    assert "SUPPRESSED" in out and "entity_grounding" in out
    assert "ClickHouse" in out
    assert "now in our stack" not in out
    kinds = [e["kind"] for e in journal.read_events(limit=50)]
    assert "report_suppressed" in kinds


def test_report_emits_the_composed_text_when_both_guards_allow(data_dir, monkeypatch, capsys):
    import cognition.models.ollama as ollama_mod

    journal.append_event("cycle", {"trigger_count": 1,
                                   "winner": {"source_module": "repo", "content": "issue #12"}})
    monkeypatch.setattr(ollama_mod, "ollama_query", lambda *a, **kw: "ALLOW\nfine")

    rc = cli_tools.cmd_report(_ns(minutes=60, json=False))

    out = capsys.readouterr().out
    assert rc == 0
    assert "1 cycle" in out and "repo" in out


def test_report_with_an_empty_window_says_so(data_dir, capsys):
    rc = cli_tools.cmd_report(_ns(minutes=60, json=False))
    assert rc == 0
    assert "nothing" in capsys.readouterr().out.lower()


# --- self-mod: the agent's own override file, under cooldown -------------------------

def test_a_human_at_the_cli_is_not_blocked_by_the_agents_cooldown(
        data_dir, monkeypatch, capsys):
    """The operator can always correct the agent.

    This used to refuse the second write with "cooling". One lock was doing
    two jobs -- bounding an autonomous loop that rewrites the agent's own
    system prompt, AND bounding the human fixing it -- so an agent proposal
    could leave a person unable to write a correction for 24h without
    deleting a sqlite file. Incident response should not require knowing
    that (Buga's ruling, 2026-08-29). The CLI passes ACTOR_HUMAN; the
    cooldown and the arming window now bind only the agent.
    """
    monkeypatch.setenv("ZUGAMIND_SELF_MOD_ENABLED", "true")
    from cognition import self_mod
    text_file = data_dir / "new_override.md"
    text_file.write_text("Prefer the smaller fix.", encoding="utf-8")

    rc1 = cli_tools.cmd_self_mod(_ns(facet="deliberative", text_file=str(text_file),
                                     why="reflection said so", evidence="reflection@1"))
    out1 = capsys.readouterr().out

    text_file.write_text("No, prefer the bigger fix.", encoding="utf-8")
    rc2 = cli_tools.cmd_self_mod(_ns(facet="deliberative", text_file=str(text_file),
                                     why="correcting myself", evidence="reflection@2"))
    out2 = capsys.readouterr().out

    assert rc1 == 0 and "applied" in out1
    assert rc2 == 0 and "applied" in out2, "a human must never be cooled out"
    assert self_mod.override_path("deliberative").read_text(
        encoding="utf-8") == "No, prefer the bigger fix."


def test_the_agent_is_still_cooled_after_a_human_write(data_dir, monkeypatch):
    """The mirror, and the reason the split is safe: exempting the human does
    not exempt the loop. The agent still gets one write per 24h."""
    monkeypatch.setenv("ZUGAMIND_SELF_MOD_ENABLED", "true")
    from cognition import self_mod
    self_mod.arm(now=1_000.0)

    first = self_mod.propose("deliberative", "agent line one", why="w",
                             actor=self_mod.ACTOR_AGENT, now=1_000.0)
    second = self_mod.propose("deliberative", "agent line two", why="w",
                              actor=self_mod.ACTOR_AGENT, now=1_000.0 + 60)

    assert first["applied"] is True
    assert second["applied"] is False and second["reason"] == "cooling"


# --- parse_when ----------------------------------------------------------------------

def test_parse_when_relative_and_today():
    now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    assert cli_tools.parse_when("90m", now) == "2026-08-29T10:30:00+00:00"
    assert cli_tools.parse_when("2d", now) == "2026-08-27T12:00:00+00:00"
    assert cli_tools.parse_when("today", now) == "2026-08-29T00:00:00+00:00"
    assert cli_tools.parse_when("2026-08-29T11:00:00Z", now) == "2026-08-29T11:00:00+00:00"
    assert cli_tools.parse_when(None) is None


# --- budget ------------------------------------------------------------------------------

def test_budget_prints_the_ledger_against_the_cap(data_dir, capsys):
    b = budget.load_budget()
    budget.record_spend(b, "sonnet", cost=1.25)
    cli_tools.cmd_budget(_ns(json=False))
    out = capsys.readouterr().out
    assert "spent: $1.2500" in out and "sonnet=1" in out and "cap:" in out


def test_budget_json_is_the_ledgers_own_shape(data_dir, capsys):
    cli_tools.cmd_budget(_ns(json=True))
    data = json.loads(capsys.readouterr().out)
    assert {"month", "spent", "remaining", "calls", "cap"} <= set(data)


# --- logs ----------------------------------------------------------------------------------

def _seed(monkeypatch, n=5):
    for i in range(n):
        monkeypatch.setattr(journal, "now_iso", lambda i=i: f"2026-08-29T0{i}:00:00+00:00")
        journal.append_event("cycle", {"winner": {"source_module": f"m{i}", "content": "c", "salience": 0.5}, "bids": []})
        journal.append_event("alarm", {"detail": f"alarm {i}"})


def test_logs_last_n_and_kind_filter(data_dir, monkeypatch, capsys):
    _seed(monkeypatch)
    cli_tools.cmd_logs(_ns(n=3, since=None, until=None, kind="alarm", json=False, follow=False))
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 3 and all("ALARM: alarm" in l for l in lines)
    assert lines[-1].endswith("alarm 4")


def test_logs_since_until_and_json(data_dir, monkeypatch, capsys):
    _seed(monkeypatch)
    cli_tools.cmd_logs(_ns(n=0, since="2026-08-29T01:30:00+00:00", until="2026-08-29T03:30:00+00:00",
                           kind=None, json=True, follow=False))
    rows = [json.loads(l) for l in capsys.readouterr().out.strip().splitlines()]
    assert [r["ts"][11:13] for r in rows] == ["02", "02", "03", "03"]


def test_logs_reads_across_segments(data_dir, monkeypatch, capsys):
    (data_dir / "journal.1.jsonl").write_text('{"ts": "2026-08-28T00:00:00+00:00", "kind": "cycle_error", "error": "old"}\n', encoding="utf-8")
    _seed(monkeypatch, 1)
    cli_tools.cmd_logs(_ns(n=0, since=None, until=None, kind=None, json=False, follow=False))
    out = capsys.readouterr().out
    assert "CYCLE ERROR: old" in out and "alarm 0" in out


# --- doctor --------------------------------------------------------------------------------

def test_doctor_on_an_empty_install_fails_on_config_and_exits_1(data_dir, capsys):
    rc = cli_tools.cmd_doctor(_ns(fix=False, json=False))
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL] harness config" in out and "examples/harness-configs" in out
    assert "[WARN] ANTHROPIC_API_KEY" in out
    assert "[OK  ] data dir" in out


def test_doctor_with_a_sane_config_passes(data_dir, monkeypatch, capsys):
    import sys
    (data_dir / "harness.json").write_text(json.dumps([{
        "name": "h", "command": [sys.executable, "-c", "print(open('{briefing_file}').read())"],
        "enabled": True, "wake_modules": ["repo_issues"]}]), encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-NOT-REAL")
    monkeypatch.setattr(cli_tools, "_ollama_check", lambda: (True, "reachable, model installed"))
    rc = cli_tools.cmd_doctor(_ns(fix=False, json=True))
    checks = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert all(c["level"] != "FAIL" for c in checks)
    assert any(c["check"] == "harness h command" and c["level"] == "OK" for c in checks), json.dumps(checks, indent=1)


def test_doctor_flags_unknown_wake_modules_and_missing_placeholder(data_dir, monkeypatch, capsys):
    import sys
    (data_dir / "harness.json").write_text(json.dumps([{
        "name": "h", "command": [sys.executable, "-c", "pass"], "enabled": True, "wake_modules": ["nope"]}]),
        encoding="utf-8")
    monkeypatch.setattr(cli_tools, "_ollama_check", lambda: (True, "ok"))
    cli_tools.cmd_doctor(_ns(fix=False, json=False))
    out = capsys.readouterr().out
    assert "unknown module(s) ['nope']" in out and "no {briefing_file} placeholder" in out


def test_doctor_reports_torn_and_malformed_journal(data_dir, monkeypatch, capsys):
    (data_dir / "journal.jsonl").write_bytes(b'{"ts": "t", "kind": "cycle"}\n}\n{"ts": "t2", "ki')
    monkeypatch.setattr(cli_tools, "_ollama_check", lambda: (True, "ok"))
    cli_tools.cmd_doctor(_ns(fix=False, json=False))
    out = capsys.readouterr().out
    assert "torn tail" in out and "malformed line(s)" in out


def test_doctor_fix_removes_a_stale_pid_file(data_dir, monkeypatch, capsys):
    (data_dir / "daemon.pid").write_text("999999999", encoding="utf-8")
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(cli_tools, "_ollama_check", lambda: (True, "ok"))
    cli_tools.cmd_doctor(_ns(fix=False, json=False))
    assert "stale PID file" in capsys.readouterr().out
    cli_tools.cmd_doctor(_ns(fix=True, json=False))
    assert "removed" in capsys.readouterr().out and not (data_dir / "daemon.pid").exists()


def test_doctor_warns_when_paused_and_at_ceiling(data_dir, monkeypatch, capsys):
    (data_dir / "PAUSE").write_text("", encoding="utf-8")
    (data_dir / "floor_calibration.json").write_text(json.dumps({"h": {"samples": [0.99] * 20, "floor": 0.9}}), encoding="utf-8")
    monkeypatch.setattr(cli_tools, "_ollama_check", lambda: (True, "ok"))
    cli_tools.cmd_doctor(_ns(fix=False, json=False))
    out = capsys.readouterr().out
    assert "[WARN] pause" in out and "AT CEILING" in out


# --- explain ---------------------------------------------------------------------------------

def test_explain_last_cycle_and_what_followed(data_dir, monkeypatch, capsys):
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [])
    journal.append_event("cycle", {"winner": {"source_module": "repo_issues", "content": "#7 bug", "salience": 0.66,
                                              "context": {"raw_salience": 0.5}, "runner_up_module": "world_signals"},
                                   "bids": [{"module": "repo_issues", "salience": 0.66}, {"module": "world_signals", "salience": 0.3}]})
    journal.append_event("harness_invocation", {"harness": "claude-code", "ok": True})
    journal.append_event("cycle", {"winner": None, "bids": []})
    lines = cli_tools.explain_cycle("last")
    text = "\n".join(lines)
    assert "winner: repo_issues" in text and "the module bid 0.500" in text and "raised" in text
    assert "runner-up: world_signals" in text
    assert "wake: claude-code ok" in text


def test_explain_names_the_gate_verdict(data_dir, monkeypatch):
    monkeypatch.setattr(command_actuator, "load_harness_configs", lambda *a, **kw: [
        {"name": "cc", "enabled": True, "wake_modules": ["code_changes"], "command": ["x"]},
        {"name": "other", "enabled": True, "wake_modules": ["knowledge"], "command": ["x"]},
    ])
    monkeypatch.setattr(journal, "_wake_gate_hints", lambda module, harnesses: [("cc", 0.57, "raw"), ("other", 0.3, "modulated")])
    journal.append_event("cycle", {"winner": {"source_module": "code_changes", "content": "c", "salience": 0.224,
                                              "context": {"raw_salience": 0.32}}, "bids": []})
    journal.append_event("wake_filtered", {"winner_module": "code_changes", "salience": 0.224})
    text = "\n".join(cli_tools.explain_cycle("last"))
    assert "gate cc: raw 0.320 < floor 0.570 (short by 0.250) -> cannot wake" in text
    assert "gate other: module filter ['knowledge'] does not include code_changes -> cannot wake" in text
    assert "filtered: code_changes 0.224 — no enabled harness accepted it" in text


def test_summarize_event_covers_every_journaled_kind_without_raising():
    for kind in cli_tools._EVENT_KINDS:
        line = cli_tools.summarize_event({"ts": "t", "kind": kind})
        assert isinstance(line, str) and line


def test_doctor_names_the_data_dir_first(data_dir, monkeypatch, capsys):
    monkeypatch.setattr(cli_tools, "_ollama_check", lambda: (True, "ok"))
    cli_tools.cmd_doctor(_ns(fix=False, json=False))
    first = capsys.readouterr().out.splitlines()[0]
    assert first.startswith("[OK  ] data dir:") and str(data_dir) in first


def test_explain_with_no_cycles(data_dir):
    assert cli_tools.explain_cycle("last") == ["no cycle with a winner in the journal yet"]


# --- verify + registration --------------------------------------------------------------------

def test_all_tools_are_registered_in_the_main_parser(monkeypatch, capsys):
    for name in ("doctor", "logs", "budget", "explain", "verify"):
        with pytest.raises(SystemExit) as e:
            cli.main([name, "--help"])
        assert e.value.code == 0
        assert name in capsys.readouterr().out


def test_render_markdown_handles_headings_bullets_and_inline():
    out = cli.render_markdown("# Title\n- item with **bold** and `code`\nplain", width=60)
    assert "Title" in out and "item with" in out and "plain" in out
    assert "**" not in out and "`" not in out
