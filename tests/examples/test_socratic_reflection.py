"""Tests for the Socratic reflection reference example (examples/socratic_reflection/).

Moved here with the code itself per issue #4 — this pipeline had zero test
coverage before the move; these are new, not relocated.
"""
from __future__ import annotations

import subprocess

from examples.socratic_reflection.answer_router import answer_question, _extract_keywords
from examples.socratic_reflection.domain_classifier import classify_domain
from examples.socratic_reflection.question_generator import generate_question


# --- domain_classifier -------------------------------------------------------

def test_classify_domain_self_keyword():
    result = classify_domain({"text": "the workspace cycle_complete event fired"}, use_llm=False)
    assert result["domain"] == "SELF"
    assert result["method"] == "keyword"


def test_classify_domain_operational_keyword():
    result = classify_domain({"text": "new customer churn on the subscription"}, use_llm=False)
    assert result["domain"] == "OPERATIONAL"


def test_classify_domain_external_keyword():
    result = classify_domain({"text": "new arxiv_paper on llm hallucination"}, use_llm=False)
    assert result["domain"] == "EXTERNAL"


def test_classify_domain_lens_routing_beats_keywords():
    result = classify_domain({"where": "external:reddit", "text": "revenue"}, use_llm=False)
    assert result["domain"] == "EXTERNAL"
    assert result["method"] == "lens"


def test_classify_domain_default_when_no_signal():
    result = classify_domain({}, use_llm=False)
    assert result == {"domain": "OPERATIONAL", "confidence": 0.0, "method": "default"}


# --- question_generator -------------------------------------------------------

def test_generate_question_parses_valid_response():
    def fake_ollama(prompt, max_tokens=120, system=""):
        return '{"text": "Why did the workspace_winner change?", "answer_source_hint": "code_search"}'

    q = generate_question({"text": "workspace winner"}, "SELF", ollama_query_fn=fake_ollama)
    assert q == {"text": "Why did the workspace_winner change?", "answer_source_hint": "code_search"}


def test_generate_question_none_on_unparseable_response():
    q = generate_question({"text": "x"}, "SELF", ollama_query_fn=lambda *a, **k: "not json at all")
    assert q is None


def test_generate_question_none_on_model_failure():
    def raising(*a, **k):
        raise RuntimeError("model down")

    assert generate_question({"text": "x"}, "SELF", ollama_query_fn=raising) is None


def test_generate_question_drops_unanswerable_code_search():
    # "hi ok to" are all stopwords/too-short — no extractable keyword.
    def fake_ollama(prompt, max_tokens=120, system=""):
        return '{"text": "is it ok?", "answer_source_hint": "code_search"}'

    assert generate_question({"text": "x"}, "SELF", ollama_query_fn=fake_ollama) is None


def test_generate_question_invalid_hint_defaults_to_none():
    def fake_ollama(prompt, max_tokens=120, system=""):
        return '{"text": "What changed?", "answer_source_hint": "carrier_pigeon"}'

    q = generate_question({"text": "x"}, "SELF", ollama_query_fn=fake_ollama)
    assert q["answer_source_hint"] == "none"


# --- answer_router -------------------------------------------------------

def test_extract_keywords_skips_stopwords_and_short_tokens():
    kws = _extract_keywords("What is the workspace_winner and why did it change?", k=3)
    assert "workspace_winner" in kws
    assert "what" not in [k.lower() for k in kws]


def test_answer_question_code_search_no_keywords_fails_fast():
    r = answer_question("is it ok?", "code_search")
    assert r["source"] == "code_search"
    assert r["success"] is False
    assert r["meta"]["keywords"] == []


def test_answer_question_code_search_success(monkeypatch):
    def fake_run(cmd, cwd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout="foo.py:12:def workspace_winner():\n", stderr="")

    monkeypatch.setattr("examples.socratic_reflection.answer_router.subprocess.run", fake_run)
    r = answer_question("what is workspace_winner?", "code_search")
    assert r["success"] is True
    assert "workspace_winner" in r["content"]
    assert r["meta"]["matches"] == 1


def test_answer_question_code_search_no_matches(monkeypatch):
    def fake_run(cmd, cwd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr("examples.socratic_reflection.answer_router.subprocess.run", fake_run)
    r = answer_question("what is workspace_winner?", "code_search")
    assert r["success"] is True
    assert r["meta"]["matches"] == 0


def test_answer_question_file_read_is_a_stub():
    r = answer_question("anything", "file_read")
    assert r["source"] == "file_read"
    assert r["success"] is False


def test_answer_question_none_source():
    r = answer_question("anything", "none")
    assert r["source"] == "none"
    assert r["content"] == ""


def test_answer_question_invalid_hint_falls_back_to_none():
    r = answer_question("anything", "carrier_pigeon")
    assert r["source"] == "none"
