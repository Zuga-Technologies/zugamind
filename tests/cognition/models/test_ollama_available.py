"""Boot-probe tests for `ollama_available` (2026-08-16, BugaPC issue #22).

The probe used to match only the model FAMILY (`LOCAL_MODEL.split(":")[0]`),
so `qwen2.5:14b-instruct` was reported available on a box that only had
`qwen2.5:7b-instruct`. Boot passed, every real call 404'd, and the daemon
stopped journalling silently for hours. Both directions are pinned here so the
probe can't quietly weaken back into a prefix match.
"""
import json
from io import BytesIO
from unittest.mock import patch

from cognition.models import ollama as ollama_mod
from cognition.models.ollama import ollama_available


class _FakeResponse:
    """Mirrors the slice of http.client.HTTPResponse the client uses — it IS
    a context manager (the client closes it with `with`, 2026-08-28)."""

    def __init__(self, payload: dict):
        self._buf = BytesIO(json.dumps(payload).encode())

    def read(self):
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _tags(*names):
    return _FakeResponse({"models": [{"name": n} for n in names]})


def test_family_match_is_not_enough():
    """The exact bug: same family, different tag -> NOT available."""
    with patch.object(ollama_mod, "LOCAL_MODEL", "qwen2.5:14b-instruct"), \
         patch("cognition.models.ollama.urlopen",
               return_value=_tags("qwen2.5:7b-instruct", "qwen3:8b")):
        assert ollama_available() is False


def test_exact_tag_is_available():
    with patch.object(ollama_mod, "LOCAL_MODEL", "qwen2.5:7b-instruct"), \
         patch("cognition.models.ollama.urlopen",
               return_value=_tags("qwen2.5:7b-instruct", "qwen3:8b")):
        assert ollama_available() is True


def test_bare_name_matches_latest_tag():
    """`nomic-embed-text` and `nomic-embed-text:latest` are the same model."""
    with patch.object(ollama_mod, "LOCAL_MODEL", "nomic-embed-text"), \
         patch("cognition.models.ollama.urlopen",
               return_value=_tags("nomic-embed-text:latest")):
        assert ollama_available() is True


def test_missing_model_is_logged_loudly(caplog):
    """A silent False is what made #22 invisible -- the name must be logged."""
    with patch.object(ollama_mod, "LOCAL_MODEL", "qwen2.5:14b-instruct"), \
         patch("cognition.models.ollama.urlopen",
               return_value=_tags("qwen2.5:7b-instruct")):
        with caplog.at_level("ERROR"):
            assert ollama_available() is False

    assert "qwen2.5:14b-instruct" in caplog.text
    assert "qwen2.5:7b-instruct" in caplog.text


def test_ollama_down_is_false_and_does_not_raise():
    with patch("cognition.models.ollama.urlopen",
               side_effect=ConnectionRefusedError("down")):
        assert ollama_available() is False
