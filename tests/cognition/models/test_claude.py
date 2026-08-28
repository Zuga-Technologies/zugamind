"""cognition/models/claude.py — zero coverage before 2026-08-28.

No network: urlopen is replaced. The fake API key below is not a real key.
"""
from __future__ import annotations

import email.message
import json
import logging
import urllib.error
from io import BytesIO
from unittest.mock import patch

import pytest

import cognition.models.claude as claude

FAKE_KEY = "sk-ant-test-NOT-A-REAL-KEY-0000"


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok(text="yes", stop="end_turn", model="claude-haiku-4-5", usage=None, lead=()):
    return {"model": model, "stop_reason": stop,
            "usage": usage or {"input_tokens": 1000, "output_tokens": 100},
            "content": list(lead) + [{"type": "text", "text": text}]}


def _http_error(code, body=None, retry_after=None):
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["retry-after"] = str(retry_after)
    payload = json.dumps(body if body is not None else {"type": "error", "error": {"type": "x", "message": "m"}}).encode()
    return urllib.error.HTTPError(claude.API_URL, code, "err", hdrs, BytesIO(payload))


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    sleeps = []
    monkeypatch.setattr(claude.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


def _scripted(responses):
    """urlopen fake: each entry is a payload dict, an exception, or a callable(req)."""
    calls = []

    def fake(req, timeout=None):
        calls.append(req)
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return _Resp(item(req))
        return _Resp(item)
    fake.calls = calls
    return fake


# --- basics ----------------------------------------------------------------

def test_no_key_returns_none_without_a_request(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake = _scripted([_ok()])
    with patch("cognition.models.claude.urlopen", fake):
        assert claude.query_claude_api("hi", "claude-haiku-4-5") is None
    assert fake.calls == []


def test_success_returns_text_and_fills_usage_out(env):
    fake = _scripted([_ok("yes", lead=[{"type": "thinking", "thinking": ""}])])
    out = {}
    with patch("cognition.models.claude.urlopen", fake):
        assert claude.query_claude_api("hi", "claude-haiku-4-5", usage_out=out) == "yes"
    assert out["model"] == "claude-haiku-4-5"
    assert out["usage"] == {"input_tokens": 1000, "output_tokens": 100}
    assert out["cost_usd"] == pytest.approx((1000 * 1.0 + 100 * 5.0) / 1e6)
    assert out["attempts"] == 1 and out["truncated"] is False


@pytest.mark.parametrize("model,expect_thinking", [
    ("claude-sonnet-5", {"type": "disabled"}),
    ("claude-opus-5", {"type": "disabled"}),
    ("claude-haiku-4-5", None),          # never adaptive when omitted: no param at all
    ("claude-opus-4-8", None),
    ("claude-fable-5", None),            # rejects an explicit disable
    ("claude-mythos-5", None),
])
def test_thinking_param_only_where_omitting_means_thinking_on(env, model, expect_thinking):
    seen = {}

    def capture(req):
        seen.update(json.loads(req.data))
        return _ok(model=model)
    with patch("cognition.models.claude.urlopen", _scripted([capture])):
        claude.query_claude_api("hi", model, system="sys")
    assert seen.get("thinking") == expect_thinking
    assert seen["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert seen["max_tokens"] == 500


def test_request_headers_are_exactly_the_three(env):
    fake = _scripted([_ok()])
    with patch("cognition.models.claude.urlopen", fake):
        claude.query_claude_api("hi", "claude-haiku-4-5")
    req = fake.calls[0]
    assert {k.lower() for k in req.headers} == {"x-api-key", "anthropic-version", "content-type"}


# --- stop reasons ------------------------------------------------------------

def test_refusal_returns_none_and_logs_the_category(env, caplog):
    body = {"model": "claude-opus-5", "stop_reason": "refusal", "usage": {"input_tokens": 5, "output_tokens": 0},
            "stop_details": {"type": "refusal", "category": "cyber", "explanation": "no"}, "content": []}
    out = {}
    with patch("cognition.models.claude.urlopen", _scripted([body])), caplog.at_level(logging.WARNING):
        assert claude.query_claude_api("hi", "claude-opus-5", usage_out=out) is None
    assert out["stop_reason"] == "refusal"
    assert any("refused" in r.message and "cyber" in r.message for r in caplog.records)


def test_max_tokens_truncation_is_returned_but_flagged(env, caplog):
    out = {}
    with patch("cognition.models.claude.urlopen", _scripted([_ok("half an ans", stop="max_tokens")])), \
            caplog.at_level(logging.WARNING):
        assert claude.query_claude_api("hi", "claude-haiku-4-5", usage_out=out) == "half an ans"
    assert out["truncated"] is True
    assert any("truncated" in r.message for r in caplog.records)


# --- retries -------------------------------------------------------------------

def test_429_honors_retry_after_then_succeeds(env):
    fake = _scripted([_http_error(429, retry_after=2), _ok("ok")])
    out = {}
    with patch("cognition.models.claude.urlopen", fake):
        assert claude.query_claude_api("hi", "claude-haiku-4-5", usage_out=out) == "ok"
    assert env == [2.0] and out["attempts"] == 2


def test_400_is_not_retried_and_the_error_message_is_logged_without_the_key(env, caplog):
    fake = _scripted([_http_error(400, {"type": "error", "error": {
        "type": "invalid_request_error", "message": "thinking: disabled is not supported here"}})])
    with patch("cognition.models.claude.urlopen", fake), caplog.at_level(logging.WARNING):
        assert claude.query_claude_api("hi", "claude-haiku-4-5") is None
    assert len(fake.calls) == 1 and env == []
    assert any("thinking: disabled is not supported here" in r.message for r in caplog.records)
    assert FAKE_KEY not in caplog.text


def test_529_exhausts_retries_then_returns_none(env):
    fake = _scripted([_http_error(529)])
    with patch("cognition.models.claude.urlopen", fake):
        assert claude.query_claude_api("hi", "claude-haiku-4-5") is None
    assert len(fake.calls) == claude._MAX_RETRIES + 1
    assert env == [1.0, 2.0]


def test_connection_error_is_retried(env):
    fake = _scripted([urllib.error.URLError("refused"), _ok("ok")])
    with patch("cognition.models.claude.urlopen", fake):
        assert claude.query_claude_api("hi", "claude-haiku-4-5") == "ok"


def test_never_raises_on_garbage_body(env):
    class _Bad(_Resp):
        def read(self):
            return b"not json"
    with patch("cognition.models.claude.urlopen", lambda req, timeout=None: _Bad({})):
        assert claude.query_claude_api("hi", "claude-haiku-4-5") is None


# --- pricing ---------------------------------------------------------------------

def test_cost_uses_family_prefix_and_cache_multipliers():
    usage = {"input_tokens": 0, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 1000, "output_tokens": 0}
    # opus-4-8 input $5: 1000*5*0.1 + 1000*5*1.25 = 6750 per-million-dollars
    assert claude.estimate_cost_usd("claude-opus-4-8", usage) == pytest.approx(0.00675)
    assert claude.estimate_cost_usd("claude-haiku-4-5-20251001", {"input_tokens": 1_000_000}) == pytest.approx(1.0)
    assert claude.estimate_cost_usd("claude-unknown-9", {"input_tokens": 10}) is None


def test_price_override_env(monkeypatch):
    monkeypatch.setenv("ZUGAMIND_CLAUDE_PRICES", json.dumps({"claude-custom-1": [100, 200]}))
    assert claude.estimate_cost_usd("claude-custom-1", {"input_tokens": 1_000_000}) == pytest.approx(100.0)
