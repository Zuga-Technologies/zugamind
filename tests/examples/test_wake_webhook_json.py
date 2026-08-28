"""Tests for examples/integrations/wake_webhook_json.py — the JSON-envelope
webhook wake for n8n / Zapier / Make. All HTTP is monkeypatched."""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

_INTEGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "examples" / "integrations"
if str(_INTEGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_INTEGRATIONS_DIR))

import wake_webhook_json  # noqa: E402


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture_posts(monkeypatch, results):
    """results: list of ints (status) or Exceptions to raise, consumed in order."""
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        outcome = results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        resp = _FakeResponse()
        resp.status = outcome
        return resp

    monkeypatch.setattr(wake_webhook_json.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(wake_webhook_json.time, "sleep", lambda s: None)
    return calls


def _briefing(tmp_path) -> Path:
    p = tmp_path / "briefing.md"
    p.write_text("# Wake\nline one\nline two", encoding="utf-8")
    return p


def _journal_with_cycle(monkeypatch, tmp_path):
    j = tmp_path / "journal.jsonl"
    j.write_text(
        json.dumps({"kind": "harness_invocation", "ok": True}) + "\n"
        + json.dumps({"kind": "cycle", "trigger_count": 4,
                      "winner": {"source_module": "repo_issues", "salience": 0.83,
                                 "thought_type": "external"}}) + "\n"
        + json.dumps({"kind": "harness_invocation", "ok": True}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(wake_webhook_json, "_JOURNAL", j)


def test_success_posts_envelope_with_winner(monkeypatch, tmp_path, capsys):
    calls = _capture_posts(monkeypatch, [200])
    _journal_with_cycle(monkeypatch, tmp_path)
    rc = wake_webhook_json.main([str(_briefing(tmp_path)), "--url", "https://n8n.local/hook",
                                 "--header", "X-Zuga-Secret: abc123"])
    assert rc == 0
    (req,) = calls
    body = json.loads(req.data.decode("utf-8"))
    assert body["source"] == "zugamind" and body["event"] == "wake"
    assert body["briefing"].startswith("# Wake")
    assert body["winner"]["module"] == "repo_issues"
    assert body["winner"]["salience"] == 0.83
    assert body["winner"]["trigger_count"] == 4
    assert req.headers["Content-type"] == "application/json"
    assert req.headers["X-zuga-secret"] == "abc123"
    assert "winner=repo_issues" in capsys.readouterr().out


def test_missing_journal_degrades_to_null_winner(monkeypatch, tmp_path):
    calls = _capture_posts(monkeypatch, [200])
    monkeypatch.setattr(wake_webhook_json, "_JOURNAL", tmp_path / "nope.jsonl")
    rc = wake_webhook_json.main([str(_briefing(tmp_path)), "--url", "https://x.local/h"])
    assert rc == 0
    assert json.loads(calls[0].data)["winner"] is None


def test_http_4xx_fails_without_retry(monkeypatch, tmp_path):
    err = urllib.error.HTTPError("u", 400, "bad", None, io.BytesIO(b""))
    calls = _capture_posts(monkeypatch, [err])
    monkeypatch.setattr(wake_webhook_json, "_JOURNAL", tmp_path / "nope.jsonl")
    rc = wake_webhook_json.main([str(_briefing(tmp_path)), "--url", "https://x.local/h"])
    assert rc == 1
    assert len(calls) == 1  # 4xx is not retried


def test_transient_5xx_retries_then_succeeds(monkeypatch, tmp_path):
    err = urllib.error.HTTPError("u", 503, "unavailable", None, io.BytesIO(b""))
    calls = _capture_posts(monkeypatch, [err, 200])
    monkeypatch.setattr(wake_webhook_json, "_JOURNAL", tmp_path / "nope.jsonl")
    rc = wake_webhook_json.main([str(_briefing(tmp_path)), "--url", "https://x.local/h"])
    assert rc == 0
    assert len(calls) == 2


def test_missing_briefing_fails_before_any_post(monkeypatch, tmp_path):
    calls = _capture_posts(monkeypatch, [])
    rc = wake_webhook_json.main([str(tmp_path / "gone.md"), "--url", "https://x.local/h"])
    assert rc == 1 and calls == []


def test_no_url_fails(monkeypatch, tmp_path):
    monkeypatch.delenv("ZUGAMIND_WEBHOOK_URL", raising=False)
    rc = wake_webhook_json.main([str(_briefing(tmp_path))])
    assert rc == 1


def test_n8n_template_is_valid_json_and_wired():
    tpl = json.loads((_INTEGRATIONS_DIR / "n8n-zugamind-wake.workflow.json").read_text("utf-8"))
    names = {n["name"] for n in tpl["nodes"]}
    assert "ZugaMind Wake" in names and "Extract Fields" in names
    assert "ZugaMind Wake" in tpl["connections"]
