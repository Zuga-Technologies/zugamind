"""Tests for the Agent-Reach adapter example scanner
(examples/custom-scanners/agent_reach.py).

`examples/custom-scanners/` is not an importable dotted package (its dir
name has a dash and no __init__.py, matching x_activity.py / news_rss.py's
own setup) so this test inserts it onto sys.path directly, the same way
run_with_custom_scanners.py does for a real launcher.

All HTTP (urllib.request) and subprocess calls are monkeypatched — nothing
here hits the network or spawns a real mcporter process.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_SCANNER_DIR = Path(__file__).resolve().parent.parent.parent / "examples" / "custom-scanners"
if str(_SCANNER_DIR) not in sys.path:
    sys.path.insert(0, str(_SCANNER_DIR))

import agent_reach  # noqa: E402


def _use_tmp_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_reach, "_FETCH_CACHE_FILE", tmp_path / "agent_reach_fetch.json")
    monkeypatch.setattr(agent_reach, "_SEEN_FILE", tmp_path / "agent_reach_seen.json")


def _fake_clock(monkeypatch, start: float = 1000.0) -> dict:
    state = {"t": start}
    monkeypatch.setattr(agent_reach.time, "time", lambda: state["t"])
    return state


# --------------------------------------------------------------- off-switch --

def test_off_when_unconfigured(monkeypatch):
    monkeypatch.delenv("ZUGAMIND_REACH_WATCH_URLS", raising=False)
    monkeypatch.delenv("ZUGAMIND_REACH_QUERIES", raising=False)
    assert agent_reach.scan_agent_reach() == []


# ------------------------------------------------------------ web_watch: hash --

def test_first_sight_baselines_silently_then_change_triggers(monkeypatch, tmp_path):
    _use_tmp_cache(monkeypatch, tmp_path)
    monkeypatch.setenv("ZUGAMIND_REACH_WATCH_URLS", "http://example.com/page")
    monkeypatch.delenv("ZUGAMIND_REACH_QUERIES", raising=False)
    monkeypatch.setenv("ZUGAMIND_REACH_CACHE_TTL", "100")
    clock = _fake_clock(monkeypatch)

    bodies = iter(["version one content", "version one content", "version TWO content"])
    monkeypatch.setattr(agent_reach, "_fetch_jina", lambda url: next(bodies))

    # Cycle 1: nothing to compare against yet -> silent baseline, no trigger.
    assert agent_reach.scan_agent_reach() == []

    # Cycle 2: content unchanged from the baseline -> still no trigger.
    clock["t"] += 200
    assert agent_reach.scan_agent_reach() == []

    # Cycle 3: content actually changed -> exactly one reach_web_update.
    clock["t"] += 200
    out = agent_reach.scan_agent_reach()
    assert len(out) == 1
    assert out[0]["type"] == "reach_web_update"
    assert out[0]["url"] == "http://example.com/page"
    assert len(out[0]["detail"]) <= 280


def test_cache_ttl_gates_the_whole_cycle(monkeypatch, tmp_path):
    _use_tmp_cache(monkeypatch, tmp_path)
    monkeypatch.setenv("ZUGAMIND_REACH_WATCH_URLS", "http://example.com/page")
    monkeypatch.delenv("ZUGAMIND_REACH_QUERIES", raising=False)
    monkeypatch.setenv("ZUGAMIND_REACH_CACHE_TTL", "3600")
    _fake_clock(monkeypatch, start=10_000.0)  # > ttl so the cold (ts=0) cache fetches once

    calls = {"n": 0}

    def fake_fetch(url):
        calls["n"] += 1
        return f"body-{calls['n']}"

    monkeypatch.setattr(agent_reach, "_fetch_jina", fake_fetch)

    agent_reach.scan_agent_reach()  # baseline fetch
    assert calls["n"] == 1
    agent_reach.scan_agent_reach()  # still within TTL -> no fetch at all
    assert calls["n"] == 1


# -------------------------------------------------------------- search: seen --

def test_search_seen_set_dedupes_across_cycles(monkeypatch, tmp_path):
    _use_tmp_cache(monkeypatch, tmp_path)
    monkeypatch.delenv("ZUGAMIND_REACH_WATCH_URLS", raising=False)
    monkeypatch.setenv("ZUGAMIND_REACH_QUERIES", "zugamind news")
    monkeypatch.setenv("ZUGAMIND_REACH_CACHE_TTL", "100")
    monkeypatch.setenv("ZUGAMIND_REACH_SEARCH_TTL", "100")
    clock = _fake_clock(monkeypatch)

    monkeypatch.setattr(agent_reach, "_mcporter_available", lambda: True)
    monkeypatch.setattr(
        agent_reach, "_fetch_exa_results",
        lambda q: [{"url": "http://x/1", "title": "Result One", "text": "..."}],
    )

    out1 = agent_reach.scan_agent_reach()
    assert len(out1) == 1
    assert out1[0]["type"] == "reach_search_result"
    assert out1[0]["url"] == "http://x/1"

    clock["t"] += 200
    out2 = agent_reach.scan_agent_reach()
    assert out2 == []  # same (query, url) already seen -> no repeat trigger


# --------------------------------------------------------------------- cap --

def test_cap_at_five_and_overflow_stays_pending_not_lost(monkeypatch, tmp_path):
    _use_tmp_cache(monkeypatch, tmp_path)
    urls = [f"http://example.com/{i}" for i in range(8)]
    monkeypatch.setenv("ZUGAMIND_REACH_WATCH_URLS", ",".join(urls))
    monkeypatch.delenv("ZUGAMIND_REACH_QUERIES", raising=False)
    monkeypatch.setenv("ZUGAMIND_REACH_CACHE_TTL", "100")
    clock = _fake_clock(monkeypatch)

    version = {"n": 0}
    monkeypatch.setattr(agent_reach, "_fetch_jina", lambda url: f"content-{url}-v{version['n']}")

    # Cycle 1: baseline all 8 URLs silently.
    assert agent_reach.scan_agent_reach() == []

    # Cycle 2: every URL changed -> 8 candidates, capped to 5.
    clock["t"] += 200
    version["n"] = 1
    out = agent_reach.scan_agent_reach()
    assert len(out) == agent_reach._MAX_TRIGGERS == 5

    # Cycle 3 (content unchanged since cycle 2): the 5 that were emitted
    # already advanced their stored hash and stay quiet; the 3 that were
    # cut by the cap never advanced, so they still show as "changed" and
    # get their fair turn now -- nothing is silently lost to the cap.
    clock["t"] += 200
    out2 = agent_reach.scan_agent_reach()
    assert len(out2) == 3

    # Cycle 4: everything is now caught up -> quiet.
    clock["t"] += 200
    assert agent_reach.scan_agent_reach() == []


# --------------------------------------------------------- keyword relevance --

def test_keyword_relevance_scores_hits_higher(monkeypatch):
    monkeypatch.delenv("ZUGAMIND_REACH_KEYWORDS", raising=False)
    assert agent_reach._keyword_relevance("anything at all") == 0.5

    monkeypatch.setenv("ZUGAMIND_REACH_KEYWORDS", "urgent,zugamind")
    assert agent_reach._keyword_relevance("nothing matches here") == 0.2
    assert agent_reach._keyword_relevance("this is Urgent") == 0.5
    assert agent_reach._keyword_relevance("Urgent news about ZugaMind") == 0.7


# -------------------------------------------------------- mcporter-absent off --

def test_mcporter_absent_disables_search_silently(monkeypatch, tmp_path):
    _use_tmp_cache(monkeypatch, tmp_path)
    monkeypatch.delenv("ZUGAMIND_REACH_WATCH_URLS", raising=False)
    monkeypatch.setenv("ZUGAMIND_REACH_QUERIES", "anything")
    monkeypatch.setenv("ZUGAMIND_REACH_CACHE_TTL", "0")
    monkeypatch.setattr(agent_reach.shutil, "which", lambda name: None)

    def _boom(*a, **kw):
        raise AssertionError("subprocess.run must not be called when mcporter is absent")

    monkeypatch.setattr(agent_reach.subprocess, "run", _boom)

    assert agent_reach.scan_agent_reach() == []


def test_mcporter_available_helper_reflects_shutil_which(monkeypatch):
    monkeypatch.setattr(agent_reach.shutil, "which", lambda name: None)
    assert agent_reach._mcporter_available() is False
    monkeypatch.setattr(agent_reach.shutil, "which", lambda name: r"C:\tools\mcporter.exe")
    assert agent_reach._mcporter_available() is True


# ------------------------------------------------------- low-level fetch unit --

def test_fetch_jina_uses_reader_url_and_decodes_body(monkeypatch):
    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b"parsed body text"

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        return FakeResp()

    monkeypatch.setattr(agent_reach.urllib.request, "urlopen", fake_urlopen)
    body = agent_reach._fetch_jina("http://example.com/page")
    assert body == "parsed body text"
    assert captured["url"] == "https://r.jina.ai/http://example.com/page"


def test_fetch_jina_fails_silently_on_network_error(monkeypatch):
    def fake_urlopen(req, timeout):
        raise OSError("network down")

    monkeypatch.setattr(agent_reach.urllib.request, "urlopen", fake_urlopen)
    assert agent_reach._fetch_jina("http://example.com/page") is None


def test_fetch_exa_results_invokes_expected_command_and_parses(monkeypatch):
    captured = {}

    def fake_run(cmd, capture_output, text, timeout):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout=json.dumps({"results": [{"url": "http://x", "title": "t"}]}),
            stderr="",
        )

    monkeypatch.setattr(agent_reach.subprocess, "run", fake_run)
    results = agent_reach._fetch_exa_results("some query")
    assert results == [{"url": "http://x", "title": "t"}]
    assert captured["cmd"][:3] == ["mcporter", "call", "exa.web_search_exa"]
    assert "query=some query" in captured["cmd"]


def test_fetch_exa_results_fails_silently_on_bad_json(monkeypatch):
    def fake_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout="not json", stderr="")

    monkeypatch.setattr(agent_reach.subprocess, "run", fake_run)
    assert agent_reach._fetch_exa_results("q") == []


def test_fetch_exa_results_fails_silently_on_nonzero_exit(monkeypatch):
    def fake_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    monkeypatch.setattr(agent_reach.subprocess, "run", fake_run)
    assert agent_reach._fetch_exa_results("q") == []


# ------------------------------------------------------------ both channels --

def test_both_channels_combine_and_share_one_cap(monkeypatch, tmp_path):
    _use_tmp_cache(monkeypatch, tmp_path)
    urls = [f"http://example.com/{i}" for i in range(3)]
    monkeypatch.setenv("ZUGAMIND_REACH_WATCH_URLS", ",".join(urls))
    monkeypatch.setenv("ZUGAMIND_REACH_QUERIES", "q1,q2,q3")
    monkeypatch.setenv("ZUGAMIND_REACH_CACHE_TTL", "100")
    monkeypatch.setenv("ZUGAMIND_REACH_SEARCH_TTL", "100")
    clock = _fake_clock(monkeypatch)

    monkeypatch.setattr(agent_reach, "_mcporter_available", lambda: True)
    monkeypatch.setattr(
        agent_reach, "_fetch_exa_results",
        lambda q: [{"url": f"http://x/{q}", "title": q}],
    )
    version = {"n": 0}
    monkeypatch.setattr(agent_reach, "_fetch_jina", lambda url: f"content-{url}-v{version['n']}")

    # Cycle 1: web urls baseline silently; search has no baseline concept so
    # its 3 fresh results all trigger immediately.
    out = agent_reach.scan_agent_reach()
    assert len(out) == 3
    assert all(t["type"] == "reach_search_result" for t in out)

    # Cycle 2: web urls all changed now (3 more candidates) plus search is
    # fully deduped (0 more) -> total 3, well under the cap.
    clock["t"] += 200
    version["n"] = 1
    out2 = agent_reach.scan_agent_reach()
    assert len(out2) == 3
    assert all(t["type"] == "reach_web_update" for t in out2)


# ------------------------------------------------------------ tavily backend --

def test_backend_chain_prefers_mcporter_then_tavily(monkeypatch):
    monkeypatch.setattr(agent_reach, "_mcporter_available", lambda: True)
    monkeypatch.setattr(agent_reach, "_fetch_exa_results", lambda q: [{"url": "http://exa"}])
    monkeypatch.setattr(agent_reach, "_fetch_tavily_results", lambda q: [{"url": "http://tavily"}])
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    assert agent_reach._fetch_search_results("q")[0]["url"] == "http://exa"

    monkeypatch.setattr(agent_reach, "_mcporter_available", lambda: False)
    assert agent_reach._fetch_search_results("q")[0]["url"] == "http://tavily"

    monkeypatch.delenv("TAVILY_API_KEY")
    assert agent_reach._fetch_search_results("q") == []


def test_tavily_normalizes_content_to_text(monkeypatch):
    class _Resp:
        def read(self):
            return json.dumps({"results": [
                {"title": "T", "url": "http://u", "content": "body text"},
            ]}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setenv("TAVILY_API_KEY", "k")
    monkeypatch.setattr(agent_reach.urllib.request, "urlopen", lambda req, timeout: _Resp())
    out = agent_reach._fetch_tavily_results("q")
    assert out == [{"title": "T", "url": "http://u", "text": "body text"}]


def test_tavily_fails_silently_on_network_error(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    def boom(req, timeout):
        raise OSError("down")
    monkeypatch.setattr(agent_reach.urllib.request, "urlopen", boom)
    assert agent_reach._fetch_tavily_results("q") == []


def test_search_and_watch_cadences_are_independent(monkeypatch, tmp_path):
    _use_tmp_cache(monkeypatch, tmp_path)
    monkeypatch.setenv("ZUGAMIND_REACH_WATCH_URLS", "http://example.com/a")
    monkeypatch.setenv("ZUGAMIND_REACH_QUERIES", "q")
    monkeypatch.setenv("ZUGAMIND_REACH_CACHE_TTL", "100")
    monkeypatch.setenv("ZUGAMIND_REACH_SEARCH_TTL", "10000")
    clock = _fake_clock(monkeypatch, start=50_000.0)

    monkeypatch.setattr(agent_reach, "_mcporter_available", lambda: False)
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    calls = {"n": 0}
    def fake_tavily(q):
        calls["n"] += 1
        return [{"url": f"http://t/{calls['n']}", "title": "t", "text": ""}]
    monkeypatch.setattr(agent_reach, "_fetch_tavily_results", fake_tavily)
    version = {"n": 0}
    monkeypatch.setattr(agent_reach, "_fetch_jina", lambda url: f"v{version['n']}")

    agent_reach.scan_agent_reach()          # both run: baseline + search #1
    assert calls["n"] == 1
    clock["t"] += 200                        # watch TTL expired, search TTL not
    version["n"] = 1
    out = agent_reach.scan_agent_reach()
    assert calls["n"] == 1                   # search did NOT re-poll
    assert [t["type"] for t in out] == ["reach_web_update"]
