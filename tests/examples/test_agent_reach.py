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
from datetime import datetime, timezone
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


# --------------------------------------------------- no search backend -> off --

def test_no_search_backend_disables_search_silently(monkeypatch, tmp_path):
    # Was "mcporter absent" only, and it started failing the moment the Tavily
    # REST backend landed: with a TAVILY_API_KEY in the ambient environment the
    # scan fell through to the second backend and made a REAL search request
    # from the unit suite (found 2026-08-16 — the assertion diff was live
    # Wiktionary hits). Both backends are neutralized here, and urlopen is
    # armed so a future third backend can't quietly reintroduce a network call.
    _use_tmp_cache(monkeypatch, tmp_path)
    monkeypatch.delenv("ZUGAMIND_REACH_WATCH_URLS", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("ZUGAMIND_REACH_QUERIES", "anything")
    monkeypatch.setenv("ZUGAMIND_REACH_CACHE_TTL", "0")
    monkeypatch.setattr(agent_reach.shutil, "which", lambda name: None)

    def _boom(*a, **kw):
        raise AssertionError("subprocess.run must not be called when mcporter is absent")

    def _no_network(*a, **kw):
        raise AssertionError("no search backend is configured — nothing may hit the network")

    monkeypatch.setattr(agent_reach.subprocess, "run", _boom)
    monkeypatch.setattr(agent_reach.urllib.request, "urlopen", _no_network)

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


def test_search_skips_relative_redirect_urls(monkeypatch, tmp_path):
    """Aggregator junk like /goto?url=... must not become triggers or
    pollute the seen-set (found live 2026-08-06)."""
    _use_tmp_cache(monkeypatch, tmp_path)
    monkeypatch.delenv("ZUGAMIND_REACH_WATCH_URLS", raising=False)
    monkeypatch.setenv("ZUGAMIND_REACH_QUERIES", "q")
    monkeypatch.setenv("ZUGAMIND_REACH_CACHE_TTL", "100")
    monkeypatch.setenv("ZUGAMIND_REACH_SEARCH_TTL", "100")
    _fake_clock(monkeypatch, start=50_000.0)

    monkeypatch.setattr(agent_reach, "_mcporter_available", lambda: True)
    monkeypatch.setattr(agent_reach, "_fetch_exa_results", lambda q: [
        {"url": "/goto?url=CAESjunk", "title": "redirect junk"},
        {"url": "https://real.example/post", "title": "real result"},
    ])
    out = agent_reach.scan_agent_reach()
    assert [t["url"] for t in out] == ["https://real.example/post"]
    seen = json.loads(agent_reach._SEEN_FILE.read_text())
    # seen-set is now {key: last_committed_epoch}; only the real URL's key
    # may be present — the redirect junk must not pollute it.
    assert list(seen) == ["q:https://real.example/post"]


# ------------------------------------------------- web_watch: what CHANGED --
#
# A watched page fires on a moved content hash, but the hash says only THAT
# something moved. Until 2026-08-18 the trigger reported the first 160 chars
# of the page (on anthropic.com/news: the "Skip to main content" skip-link,
# every single time) and scored relevance on the whole body — so a keyword-
# rich page bid a fixed 0.25 + 0.4*0.9 + 0.2*0.3 = 0.67 against a 0.600 wake
# floor on ANY byte change, and neither the mind nor the woken session could
# tell a new headline from a footer tweak. These cover the diff that fixed it.

def _watch_only(monkeypatch, tmp_path, ttl="100"):
    _use_tmp_cache(monkeypatch, tmp_path)
    monkeypatch.setenv("ZUGAMIND_REACH_WATCH_URLS", "http://example.com/page")
    monkeypatch.delenv("ZUGAMIND_REACH_QUERIES", raising=False)
    monkeypatch.setenv("ZUGAMIND_REACH_CACHE_TTL", ttl)
    return _fake_clock(monkeypatch)


def test_reordered_page_adds_nothing_and_stays_silent(monkeypatch, tmp_path):
    """A promo card rotating to the top moves the hash and publishes nothing."""
    clock = _watch_only(monkeypatch, tmp_path)
    first = "headline one\nheadline two\nheadline three"
    bodies = iter([first, "headline three\nheadline one\nheadline two", first])
    monkeypatch.setattr(agent_reach, "_fetch_jina", lambda url: next(bodies))

    assert agent_reach.scan_agent_reach() == []      # baseline
    clock["t"] += 200
    assert agent_reach.scan_agent_reach() == []      # reorder -> nothing added
    clock["t"] += 200
    # Baseline advanced to the reordered page, so flipping back is also silent.
    assert agent_reach.scan_agent_reach() == []


def test_detail_carries_the_addition_not_the_top_of_the_page(monkeypatch, tmp_path):
    clock = _watch_only(monkeypatch, tmp_path)
    head = "Skip to main content\nNewsroom\nolder post from last week"
    bodies = iter([head, head + "\nAug 18 2026 Product Introducing something new"])
    monkeypatch.setattr(agent_reach, "_fetch_jina", lambda url: next(bodies))

    assert agent_reach.scan_agent_reach() == []
    clock["t"] += 200
    out = agent_reach.scan_agent_reach()

    assert len(out) == 1
    assert "Introducing something new" in out[0]["detail"]
    assert "Skip to main content" not in out[0]["detail"]
    assert "+1 new" in out[0]["detail"]
    assert len(out[0]["detail"]) <= 280


def test_relevance_scores_the_addition_not_the_whole_body(monkeypatch, tmp_path):
    """The page is wall-to-wall keywords; the ADDITION is not. Scoring the body
    pinned this page at the 0.9 ceiling forever — that is the bug."""
    clock = _watch_only(monkeypatch, tmp_path)
    monkeypatch.setenv("ZUGAMIND_REACH_KEYWORDS", "claude,anthropic,agent")
    page = "claude and anthropic ship an agent\nmore claude news\nagent updates"
    bodies = iter([page, page + "\ncookie banner text refreshed"])
    monkeypatch.setattr(agent_reach, "_fetch_jina", lambda url: next(bodies))

    assert agent_reach.scan_agent_reach() == []
    clock["t"] += 200
    out = agent_reach.scan_agent_reach()

    assert len(out) == 1
    assert out[0]["relevance"] == 0.2          # keyword miss on the addition
    assert agent_reach._keyword_relevance(page) == 0.9   # ...and 0.9 on the body


def test_relevance_still_peaks_when_the_addition_is_the_news(monkeypatch, tmp_path):
    clock = _watch_only(monkeypatch, tmp_path)
    monkeypatch.setenv("ZUGAMIND_REACH_KEYWORDS", "claude,anthropic,agent")
    page = "some unrelated boilerplate\nnavigation links"
    bodies = iter([page, page + "\nAnthropic ships a new Claude agent today"])
    monkeypatch.setattr(agent_reach, "_fetch_jina", lambda url: next(bodies))

    assert agent_reach.scan_agent_reach() == []
    clock["t"] += 200
    out = agent_reach.scan_agent_reach()

    assert out[0]["relevance"] == 0.9


def test_cache_without_a_line_set_rebaselines_silently(monkeypatch, tmp_path):
    """Migration: a cache written before line-sets existed must not report the
    entire page as new on the first upgraded fetch."""
    import hashlib
    clock = _watch_only(monkeypatch, tmp_path)
    old_body = "headline one\nheadline two"
    (tmp_path / "agent_reach_fetch.json").write_text(json.dumps({
        "ts": 0, "search_ts": 0,
        "watch_hashes": {
            "http://example.com/page":
                hashlib.sha1(old_body.encode()).hexdigest()[:16],
        },
    }), encoding="utf-8")

    bodies = iter([old_body + "\nheadline three", old_body + "\nheadline four"])
    monkeypatch.setattr(agent_reach, "_fetch_jina", lambda url: next(bodies))

    # Hash moved, but there is no line-set to diff against -> silent baseline.
    assert agent_reach.scan_agent_reach() == []
    clock["t"] += 200
    out = agent_reach.scan_agent_reach()
    assert len(out) == 1
    assert "headline four" in out[0]["detail"]


def test_novelty_scales_with_how_much_was_added(monkeypatch, tmp_path):
    clock = _watch_only(monkeypatch, tmp_path)
    base = "one"
    bodies = iter([base, base + "\ntwo", base + "\ntwo\nthree\nfour\nfive\nsix"])
    monkeypatch.setattr(agent_reach, "_fetch_jina", lambda url: next(bodies))

    assert agent_reach.scan_agent_reach() == []
    clock["t"] += 200
    small = agent_reach.scan_agent_reach()[0]["novelty"]
    clock["t"] += 200
    big = agent_reach.scan_agent_reach()[0]["novelty"]
    assert small < big <= 0.85


# ------------------------------------------ search scoring (2026-08-22 / 08-28) --

def test_keyword_relevance_excludes_terms_the_query_already_contains(monkeypatch):
    monkeypatch.setenv("ZUGAMIND_REACH_KEYWORDS", "agent,open source,mcp,llm")
    text = "I built an open source memory layer for AI agents"
    # Scored bare, the text hits "agent" + "open source" -> 0.7.
    assert agent_reach._keyword_relevance(text) == 0.7
    # The query supplied both of those words; excluding them leaves no evidence.
    assert agent_reach._keyword_relevance(text, exclude="open source AI agent cognition") == 0.2
    # A keyword the query did NOT contain still counts.
    assert agent_reach._keyword_relevance(text + " with an MCP server", exclude="open source AI agent") == 0.5
    # Excluding every keyword is "no hits", not the unconfigured neutral 0.5.
    assert agent_reach._keyword_relevance(text, exclude="agent open source mcp llm") == 0.2


def test_search_relevance_ignores_the_querys_own_terms(monkeypatch, tmp_path):
    # The 2026-08-28 wake, replayed: the daemon's real keyword list and real
    # standing query against the dev.to title + a snippet mentioning LLM/MCP.
    _use_tmp_cache(monkeypatch, tmp_path)
    monkeypatch.delenv("ZUGAMIND_REACH_WATCH_URLS", raising=False)
    monkeypatch.setenv("ZUGAMIND_REACH_QUERIES", "open source AI agent cognition attention")
    monkeypatch.setenv("ZUGAMIND_REACH_KEYWORDS", "agent,claude,anthropic,mcp,llm,openai,open source")
    monkeypatch.setenv("ZUGAMIND_REACH_CACHE_TTL", "100")
    monkeypatch.setenv("ZUGAMIND_REACH_SEARCH_TTL", "100")
    _fake_clock(monkeypatch)
    monkeypatch.setattr(agent_reach, "_mcporter_available", lambda: True)
    monkeypatch.setattr(
        agent_reach, "_fetch_exa_results",
        lambda q: [{
            "url": "http://x/stash",
            "title": "I built an open-source cognitive memory layer for AI agents in Go",
            "text": "LLMs are trained to be both a reasoner and a knowledge base. MCP server included.",
        }],
    )
    (t,) = agent_reach.scan_agent_reach()
    # Bare scoring counted agent + llm + mcp = 3 hits -> the 0.9 ceiling and a
    # 0.65 bid. "agent" is the query's own word; only llm + mcp are evidence.
    assert t["relevance"] == 0.7
    # Undated -> low urgency. 0.25 + 0.4*0.7 + 0.2*0.1 = 0.55: under the
    # 0.570 floor the original 0.65 cleared.
    assert t["urgency"] == agent_reach._SEARCH_URGENCY_UNDATED


def test_search_urgency_is_publish_age_or_low_when_undated():
    now = 1_800_000_000.0
    hour = 3600.0
    assert agent_reach._search_urgency(None, now) == 0.1
    assert agent_reach._search_urgency(now - 2 * hour, now) == 0.25
    assert agent_reach._search_urgency(now - 48 * hour, now) == 0.125
    assert agent_reach._search_urgency(now - 120 * 24 * hour, now) == 0.0
    # A stamp from the future (clock skew) is "fresh", never negative age.
    assert agent_reach._search_urgency(now + hour, now) == 0.25

    april = datetime(2026, 4, 25, 2, 35, 55, tzinfo=timezone.utc).timestamp()
    assert agent_reach._parse_published("2026-04-25T02:35:55Z") == april
    assert agent_reach._parse_published("2026-04-25T02:35:55") == april   # naive -> UTC
    assert agent_reach._parse_published("2026-04-25T02:35:55+00:00") == april
    assert agent_reach._parse_published(None) is None
    assert agent_reach._parse_published("") is None
    assert agent_reach._parse_published("not a date") is None
    assert agent_reach._parse_published(1776998155) is None


def test_search_trigger_urgency_comes_from_backend_publish_date(monkeypatch, tmp_path):
    _use_tmp_cache(monkeypatch, tmp_path)
    monkeypatch.delenv("ZUGAMIND_REACH_WATCH_URLS", raising=False)
    monkeypatch.setenv("ZUGAMIND_REACH_QUERIES", "q")
    monkeypatch.setenv("ZUGAMIND_REACH_CACHE_TTL", "100")
    monkeypatch.setenv("ZUGAMIND_REACH_SEARCH_TTL", "100")
    now = datetime(2026, 8, 28, 11, 24, tzinfo=timezone.utc).timestamp()
    _fake_clock(monkeypatch, start=now)
    monkeypatch.setattr(agent_reach, "_mcporter_available", lambda: True)
    monkeypatch.setattr(agent_reach, "_fetch_exa_results", lambda q: [
        {"url": "http://x/today", "title": "t", "text": "", "publishedDate": "2026-08-28T09:00:00Z"},
        {"url": "http://x/april", "title": "t", "text": "", "publishedDate": "2026-04-25T02:35:55Z"},
        {"url": "http://x/undated", "title": "t", "text": ""},
    ])
    out = {t["url"]: t["urgency"] for t in agent_reach.scan_agent_reach()}
    assert out == {"http://x/today": 0.25, "http://x/april": 0.0, "http://x/undated": 0.1}


def test_tavily_requests_a_publish_window(monkeypatch):
    captured = {}

    class _Resp:
        def read(self):
            return json.dumps({"results": []}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setenv("TAVILY_API_KEY", "k")
    monkeypatch.setattr(agent_reach.urllib.request, "urlopen", fake_urlopen)

    monkeypatch.delenv("ZUGAMIND_REACH_SEARCH_TIME_RANGE", raising=False)
    agent_reach._fetch_tavily_results("q")
    assert captured["body"]["time_range"] == "week"  # default: a standing query asks what is NEW

    monkeypatch.setenv("ZUGAMIND_REACH_SEARCH_TIME_RANGE", "month")
    agent_reach._fetch_tavily_results("q")
    assert captured["body"]["time_range"] == "month"

    monkeypatch.setenv("ZUGAMIND_REACH_SEARCH_TIME_RANGE", "")
    agent_reach._fetch_tavily_results("q")
    assert "time_range" not in captured["body"]
