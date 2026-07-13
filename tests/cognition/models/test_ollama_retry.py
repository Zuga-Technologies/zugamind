"""Retry-on-transient-failure test for the Ollama client (2026-07-11 hardening
after a client-timeout mid-load left the Ollama scheduler refusing all
connections until restarted — see ollama_query's docstring)."""
import json
from io import BytesIO
from unittest.mock import patch

from cognition.models.ollama import ollama_query


class _FakeResponse:
    def __init__(self, payload: dict):
        self._buf = BytesIO(json.dumps(payload).encode())

    def read(self):
        return self._buf.read()


def test_retries_once_then_succeeds():
    calls = {"n": 0}

    def fake_urlopen(req, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionRefusedError("target machine actively refused it")
        return _FakeResponse({"message": {"content": "ok"}})

    with patch("cognition.models.ollama.urlopen", side_effect=fake_urlopen), \
         patch("cognition.models.ollama.time.sleep"):
        result = ollama_query("hi", model="test-model")

    assert result == "ok"
    assert calls["n"] == 2


def test_returns_none_after_all_retries_exhausted():
    def fake_urlopen(req, timeout):
        raise ConnectionRefusedError("still down")

    with patch("cognition.models.ollama.urlopen", side_effect=fake_urlopen), \
         patch("cognition.models.ollama.time.sleep"):
        result = ollama_query("hi", model="test-model", retries=1)

    assert result is None


def test_zero_retries_fails_fast_on_first_error():
    calls = {"n": 0}

    def fake_urlopen(req, timeout):
        calls["n"] += 1
        raise ConnectionRefusedError("down")

    with patch("cognition.models.ollama.urlopen", side_effect=fake_urlopen), \
         patch("cognition.models.ollama.time.sleep"):
        result = ollama_query("hi", model="test-model", retries=0)

    assert result is None
    assert calls["n"] == 1
