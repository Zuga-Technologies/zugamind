"""Tests for gates/llm_judge.py — the local-model confabulation backstop.

Covers: the empty-text short-circuit (never calls the model), fail-open
behavior when the model is unavailable (ollama_query returns None) or
errors outright, parsing of canned model verdicts (ALLOW/SUPPRESS, case
insensitivity, reason-line fallback, ambiguous first lines), the
commits=None auto-lookup path (patched so it never shells out to git), and
the never-raises contract on garbage input.

llm_judge.py has no separate "is the model available" probe — it always
calls `cognition.models.ollama.ollama_query` and treats a falsy/erroring
response as unavailable, so that's what's monkeypatched here to simulate
"no model available".
"""
from __future__ import annotations

import cognition.models.ollama as ollama_mod
import gates.llm_judge as llm_judge
from gates.llm_judge import judge_post


# --- empty text: never calls the model ---------------------------------------

def test_empty_text_returns_allow_without_calling_model(monkeypatch):
    def _must_not_be_called(*a, **kw):
        raise AssertionError("ollama_query must not be called for empty text")

    monkeypatch.setattr(ollama_mod, "ollama_query", _must_not_be_called)

    assert judge_post("", commits=[]) == {"verdict": "ALLOW", "reason": "empty"}
    assert judge_post("   ", commits=[]) == {"verdict": "ALLOW", "reason": "empty"}
    assert judge_post(None, commits=[]) == {"verdict": "ALLOW", "reason": "empty"}


# --- fail-open: model unavailable / errors -----------------------------------

def test_fail_open_when_model_returns_none(monkeypatch):
    monkeypatch.setattr(ollama_mod, "ollama_query", lambda *a, **kw: None)

    result = judge_post("I integrated ClickHouse today.", commits=[])
    assert result == {"verdict": "ALLOW", "reason": "judge_unavailable"}


def test_fail_open_when_model_returns_empty_string(monkeypatch):
    monkeypatch.setattr(ollama_mod, "ollama_query", lambda *a, **kw: "")

    result = judge_post("I integrated ClickHouse today.", commits=[])
    assert result == {"verdict": "ALLOW", "reason": "judge_unavailable"}


def test_fail_open_when_model_call_raises(monkeypatch):
    def _boom(*a, **kw):
        raise ConnectionError("ollama not running")

    monkeypatch.setattr(ollama_mod, "ollama_query", _boom)

    result = judge_post("I shipped the new dashboard.", commits=[])
    assert result == {"verdict": "ALLOW", "reason": "judge_error"}


# --- verdict parsing -----------------------------------------------------------

def test_parses_suppress_verdict_with_reason(monkeypatch):
    monkeypatch.setattr(
        ollama_mod, "ollama_query",
        lambda *a, **kw: "SUPPRESS\nNo commit backs the ClickHouse integration claim.",
    )
    result = judge_post("I integrated ClickHouse.", commits=["fix(hooks): typo"])
    assert result["verdict"] == "SUPPRESS"
    assert "ClickHouse" in result["reason"]


def test_parses_allow_verdict_with_reason(monkeypatch):
    monkeypatch.setattr(
        ollama_mod, "ollama_query",
        lambda *a, **kw: "ALLOW\nMatches recent commit about the parser.",
    )
    result = judge_post("I streamlined the parser.", commits=["perf: streamline parser"])
    assert result["verdict"] == "ALLOW"
    assert "parser" in result["reason"]


def test_verdict_parsing_is_case_insensitive(monkeypatch):
    monkeypatch.setattr(ollama_mod, "ollama_query", lambda *a, **kw: "suppress\nlowercase verdict")
    result = judge_post("claim text", commits=[])
    assert result["verdict"] == "SUPPRESS"


def test_verdict_line_with_surrounding_whitespace(monkeypatch):
    monkeypatch.setattr(ollama_mod, "ollama_query", lambda *a, **kw: "   Suppress   \nreason here")
    result = judge_post("claim text", commits=[])
    assert result["verdict"] == "SUPPRESS"


def test_single_line_suppress_falls_back_to_default_reason(monkeypatch):
    monkeypatch.setattr(ollama_mod, "ollama_query", lambda *a, **kw: "SUPPRESS")
    result = judge_post("claim text", commits=[])
    assert result == {"verdict": "SUPPRESS", "reason": "judge: unsupported claim"}


def test_single_line_allow_falls_back_to_default_reason(monkeypatch):
    monkeypatch.setattr(ollama_mod, "ollama_query", lambda *a, **kw: "ALLOW")
    result = judge_post("claim text", commits=[])
    assert result == {"verdict": "ALLOW", "reason": "judge: ok"}


def test_ambiguous_first_line_without_suppress_defaults_to_allow(monkeypatch):
    """Only "SUPPRESS" in the head line flips the verdict — anything else,
    including a garbled/unexpected first word, fails open to ALLOW."""
    monkeypatch.setattr(ollama_mod, "ollama_query", lambda *a, **kw: "MAYBE\nunclear signal")
    result = judge_post("claim text", commits=[])
    assert result["verdict"] == "ALLOW"
    assert "unclear signal" in result["reason"]


def test_reason_is_truncated_to_160_chars(monkeypatch):
    long_reason = "x" * 500
    monkeypatch.setattr(ollama_mod, "ollama_query", lambda *a, **kw: f"SUPPRESS\n{long_reason}")
    result = judge_post("claim text", commits=[])
    assert len(result["reason"]) <= 160


# --- commits=None auto-lookup path (patched to avoid shelling out to git) ----

def test_commits_none_falls_back_to_empty_when_repo_root_missing(monkeypatch):
    import gates.work_claim as work_claim

    monkeypatch.setattr(work_claim, "_repo_root", lambda: None)

    captured = {}

    def _capture(prompt, max_tokens=None, system=None):
        captured["prompt"] = prompt
        return "ALLOW\nno evidence needed"

    monkeypatch.setattr(ollama_mod, "ollama_query", _capture)

    result = judge_post("I built a new feature.", commits=None)
    assert result["verdict"] == "ALLOW"
    assert "(no commits this period)" in captured["prompt"]


def test_commits_none_falls_back_to_empty_when_lookup_raises(monkeypatch):
    import gates.work_claim as work_claim

    def _boom():
        raise RuntimeError("git unavailable")

    monkeypatch.setattr(work_claim, "_repo_root", _boom)
    monkeypatch.setattr(ollama_mod, "ollama_query", lambda *a, **kw: "ALLOW\nok")

    result = judge_post("I built a new feature.", commits=None)
    assert result == {"verdict": "ALLOW", "reason": "ok"}


def test_explicit_commits_list_is_used_verbatim_in_prompt(monkeypatch):
    captured = {}

    def _capture(prompt, max_tokens=None, system=None):
        captured["prompt"] = prompt
        return "ALLOW\nmatches"

    monkeypatch.setattr(ollama_mod, "ollama_query", _capture)

    judge_post("I streamlined the parser.", commits=["perf: streamline parser hot path"])
    assert "perf: streamline parser hot path" in captured["prompt"]


def test_window_minutes_is_reflected_in_prompt(monkeypatch):
    captured = {}

    def _capture(prompt, max_tokens=None, system=None):
        captured["prompt"] = prompt
        return "ALLOW\nok"

    monkeypatch.setattr(ollama_mod, "ollama_query", _capture)

    judge_post("some claim", commits=[], window_minutes=90)
    assert "90min" in captured["prompt"]


# --- never raises on garbage input ---------------------------------------------

def test_never_raises_on_non_string_text(monkeypatch):
    monkeypatch.setattr(ollama_mod, "ollama_query", lambda *a, **kw: "ALLOW\nok")

    result = judge_post(12345, commits=[])  # type: ignore[arg-type]
    assert result == {"verdict": "ALLOW", "reason": "judge_error"}

    result = judge_post([1, 2, 3], commits=[])  # type: ignore[arg-type]
    assert result == {"verdict": "ALLOW", "reason": "judge_error"}

    result = judge_post({"not": "text"}, commits=[])  # type: ignore[arg-type]
    assert result == {"verdict": "ALLOW", "reason": "judge_error"}


def test_never_raises_when_commits_contains_garbage(monkeypatch):
    monkeypatch.setattr(ollama_mod, "ollama_query", lambda *a, **kw: "ALLOW\nok")
    # Non-string commit entries — join()'s f-string formatting must not blow up.
    result = judge_post("I did a thing.", commits=[123, None, {"weird": True}])
    assert result["verdict"] == "ALLOW"
