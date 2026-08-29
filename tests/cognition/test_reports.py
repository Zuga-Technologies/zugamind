"""Tests for the draft-post surface (the llm_judge socket, 2026-08-29).

llm_judge.judge_post existed with no caller: no text the agent composed
about its own work ever travelled towards a human, so there was nothing for
it to judge. This is that surface. A "post" here is deliberately NOT social
media: it is a report the agent writes about what it did, destined for a
human, and it passes work_claim (free, deterministic) then llm_judge (local
model, fail-open) before it is emitted -- to the journal only, same as
thoughts (decision 1).
"""
from __future__ import annotations

import cognition.models.ollama as ollama_mod
import gates.work_claim as work_claim_mod
import pytest

from cognition import reports
from continuity import journal


def _events(kind):
    return [e for e in journal.read_events(limit=500) if e.get("kind") == kind]


@pytest.fixture
def judge_never_called(monkeypatch):
    def _must_not(*a, **kw):
        raise AssertionError("the judge must not run for a draft work_claim already refused")
    monkeypatch.setattr(ollama_mod, "ollama_query", _must_not)


# ---------------------------------------------------------------------------
# emit_report: the two guards, in order, and every drop journaled with why
# ---------------------------------------------------------------------------

def test_a_draft_claiming_work_no_commit_backs_is_suppressed_with_the_reason(judge_never_called):
    verdict = reports.emit_report("I integrated ClickHouse today.", commits=[])

    assert verdict["emitted"] is False
    assert verdict["stage"] == "work_claim"
    assert _events("report_emitted") == []
    suppressed = _events("report_suppressed")
    assert len(suppressed) == 1
    assert suppressed[0]["stage"] == "work_claim"
    assert suppressed[0]["reason"].startswith("work_claim_no_matching_commit")
    assert suppressed[0]["unbacked"] == ["I integrated ClickHouse today."]


def test_an_unavailable_judge_fails_open_and_the_report_is_emitted(monkeypatch):
    """No Ollama model (the state of BugaPC today) must mean ALLOW -- not a
    crash, and not a silent drop. The journal says the judge was absent."""
    monkeypatch.setattr(ollama_mod, "ollama_query", lambda *a, **kw: None)

    verdict = reports.emit_report("I fixed the parser.",
                                  commits=["fix: parser handles empty lines"])

    assert verdict["emitted"] is True
    emitted = _events("report_emitted")
    assert len(emitted) == 1
    assert emitted[0]["text"] == "I fixed the parser."
    assert emitted[0]["judge"] == "judge_unavailable"
    assert _events("report_suppressed") == []


def test_a_judge_suppress_verdict_stops_a_report_work_claim_let_through(monkeypatch):
    monkeypatch.setattr(ollama_mod, "ollama_query",
                        lambda *a, **kw: "SUPPRESS\nno evidence of a parser change")

    verdict = reports.emit_report("I fixed the parser.",
                                  commits=["fix: parser handles empty lines"])

    assert verdict["emitted"] is False
    assert verdict["stage"] == "judge"
    suppressed = _events("report_suppressed")
    assert len(suppressed) == 1
    assert suppressed[0]["stage"] == "judge"
    assert suppressed[0]["reason"] == "no evidence of a parser change"
    assert _events("report_emitted") == []


def test_a_crashing_judge_never_blocks_the_emit(monkeypatch):
    monkeypatch.setattr(ollama_mod, "ollama_query",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    verdict = reports.emit_report("I fixed the parser.",
                                  commits=["fix: parser handles empty lines"])

    assert verdict["emitted"] is True
    assert _events("report_emitted")[0]["judge"] == "judge_error"


# ---------------------------------------------------------------------------
# compose_report: prose from the journal, quoting what the harness said
# ---------------------------------------------------------------------------

def test_compose_report_is_empty_when_nothing_happened():
    assert reports.compose_report(window_minutes=60) == ""


def test_compose_report_counts_cycles_and_quotes_the_harness_reply():
    journal.append_event("cycle", {"trigger_count": 2,
                                   "winner": {"source_module": "repo",
                                              "content": "issue #12 reopened"}})
    journal.append_event("cycle", {"trigger_count": 0, "winner": None})
    journal.append_event("harness_invocation", {
        "harness": "claude-code", "ok": True, "dry_run": False,
        "stdout": "Fixed the parser in lexer.py so empty lines no longer crash it.",
    })
    journal.append_event("alarm", {"detail": "disk 91% full"})

    text = reports.compose_report(window_minutes=60)

    assert "2 cycles" in text and "1 acted" in text
    assert "repo" in text
    assert "claude-code" in text
    assert "Fixed the parser in lexer.py" in text
    assert "1 alarm" in text


def test_compose_report_ignores_events_outside_the_window(monkeypatch):
    journal.append_event("cycle", {"trigger_count": 1,
                                   "winner": {"source_module": "old", "content": "x"}})
    # Everything just written is "now"; a zero-minute window excludes it.
    assert reports.compose_report(window_minutes=0) == ""
