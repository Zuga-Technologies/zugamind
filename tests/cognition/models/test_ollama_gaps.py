"""cognition/models/ollama.py — the 2026-08-28 audit gaps.

Context-window sizing (Ollama silently truncated the FRONT of long prompts —
the system message — measured live on this box), retry policy by status,
error bodies in the log, and empty answers being failures not decisions.
No network: urlopen is replaced.
"""
from __future__ import annotations

import email.message
import json
import logging
import urllib.error
from io import BytesIO
from unittest.mock import patch

import pytest

import cognition.models.ollama as ollama


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok(text="fine", **extra):
    body = {"message": {"role": "assistant", "content": text}, "done": True, "done_reason": "stop"}
    body.update(extra)
    return body


def _http_error(code, message):
    return urllib.error.HTTPError("http://x/api/chat", code, "err", email.message.Message(),
                                  BytesIO(json.dumps({"error": message}).encode()))


def _scripted(items):
    calls = []

    def fake(req, timeout=None):
        calls.append(req)
        item = items[min(len(calls) - 1, len(items) - 1)]
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return _Resp(item(req))
        return _Resp(item)
    fake.calls = calls
    return fake


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(ollama.time, "sleep", lambda s: None)


# --- context window ----------------------------------------------------------

def test_num_ctx_grows_in_powers_of_two_and_is_capped(monkeypatch):
    assert ollama.num_ctx_for(100, 500) == 4096
    assert ollama.num_ctx_for(5000, 500) == 8192
    assert ollama.num_ctx_for(20000, 500) == 16384          # default OLLAMA_MAX_CTX
    monkeypatch.setattr(ollama, "OLLAMA_MAX_CTX", 32768)
    assert ollama.num_ctx_for(20000, 500) == 32768


def test_request_carries_num_ctx_sized_from_the_prompt():
    seen = {}

    def capture(req):
        seen.update(json.loads(req.data))
        return _ok()
    with patch("cognition.models.ollama.urlopen", _scripted([capture])):
        ollama.ollama_query("x" * 14000, model="m", max_tokens=500, system="be terse")
    # 14000 chars / 2 = ~7000 tokens + 500 + headroom -> 8192
    assert seen["options"]["num_ctx"] == 8192
    assert seen["options"]["num_predict"] == 500


def test_prompt_over_the_cap_warns_before_sending(caplog, monkeypatch):
    monkeypatch.setattr(ollama, "OLLAMA_MAX_CTX", 4096)
    with patch("cognition.models.ollama.urlopen", _scripted([_ok()])), caplog.at_level(logging.WARNING):
        ollama.ollama_query("x" * 30000, model="m")
    assert any("exceeds OLLAMA_MAX_CTX" in r.message for r in caplog.records)


def test_truncation_is_detected_from_the_half_window_signature(caplog):
    # a 4096 window and the server reporting exactly half of it evaluated
    # (+2, as measured live) — Ollama's overflow signature
    with patch("cognition.models.ollama.urlopen", _scripted([_ok(prompt_eval_count=2050)])), \
            caplog.at_level(logging.WARNING):
        assert ollama.ollama_query("short", model="m", max_tokens=3) == "fine"
    assert any("TRUNCATED" in r.message for r in caplog.records)


def test_normal_prompt_eval_count_does_not_warn(caplog):
    with patch("cognition.models.ollama.urlopen", _scripted([_ok(prompt_eval_count=120)])), \
            caplog.at_level(logging.WARNING):
        ollama.ollama_query("short", model="m")
    assert not any("TRUNCATED" in r.message for r in caplog.records)


# --- retry policy --------------------------------------------------------------

def test_400_is_rejected_at_once_with_the_body_in_the_log(caplog):
    fake = _scripted([_http_error(400, "invalid option num_ctx")])
    with patch("cognition.models.ollama.urlopen", fake), caplog.at_level(logging.WARNING):
        assert ollama.ollama_query("hi", model="m") is None
    assert len(fake.calls) == 1
    assert any("invalid option num_ctx" in r.message for r in caplog.records)


def test_404_gets_exactly_one_retry(caplog):
    fake = _scripted([_http_error(404, "model 'm' not found, try pulling it first")])
    with patch("cognition.models.ollama.urlopen", fake), caplog.at_level(logging.WARNING):
        assert ollama.ollama_query("hi", model="m") is None
    assert len(fake.calls) == 2
    assert any("try pulling it first" in r.message for r in caplog.records)


def test_404_then_success_recovers():
    fake = _scripted([_http_error(404, "loading"), _ok("back")])
    with patch("cognition.models.ollama.urlopen", fake):
        assert ollama.ollama_query("hi", model="m") == "back"


def test_5xx_and_connection_errors_use_the_full_retry_budget():
    fake = _scripted([_http_error(503, "busy")])
    with patch("cognition.models.ollama.urlopen", fake):
        assert ollama.ollama_query("hi", model="m", retries=3) is None
    assert len(fake.calls) == 4
    fake2 = _scripted([urllib.error.URLError("refused"), _ok("ok")])
    with patch("cognition.models.ollama.urlopen", fake2):
        assert ollama.ollama_query("hi", model="m") == "ok"


# --- answers ---------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"done": True},                                   # no message at all
    {"message": {"role": "assistant"}, "done": True},  # no content
    {"message": {"role": "assistant", "content": "   "}, "done": True},
    "not an object",
])
def test_empty_or_malformed_answer_is_none_not_empty_string(body, caplog):
    with patch("cognition.models.ollama.urlopen", _scripted([body])), caplog.at_level(logging.WARNING):
        assert ollama.ollama_query("hi", model="m") is None


def test_hitting_num_predict_is_logged_but_text_still_returned(caplog):
    with patch("cognition.models.ollama.urlopen", _scripted([_ok("partial", done_reason="length")])), \
            caplog.at_level(logging.WARNING):
        assert ollama.ollama_query("hi", model="m", max_tokens=5) == "partial"
    assert any("num_predict" in r.message for r in caplog.records)
