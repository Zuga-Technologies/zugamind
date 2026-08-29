"""Regression tests for the 2026-08-29 hackernews/reddit_ai gap-closing pass.

Named for the invariant each one proves, not the function under test. All
HTTP is mocked at the `safe_http.fetch_json` / module `_fetch_json` /
`_fetch_sub` seam — no live network calls anywhere in this suite.

Note on test_scanners_audit.py::test_urgency_cannot_leave_the_contract: gap 1
below (hackernews' new persistent seen-set) makes that test's single sweep a
cold start, so its assertion loop now runs zero iterations (it still passes,
vacuously — a fresh seen-set means scan_hackernews() returns [] on the first
call). That file is shared across this audit's scanners (ai_labs, scheduler)
and is out of this file's scope to edit; the same invariant is re-proven here
on a WARM sweep instead, see
test_hackernews_title_control_characters_and_bad_numbers_are_neutralized_before_detail.
"""
from __future__ import annotations

import json
import time as time_module

import pytest

from zugamind.scanners.world import hackernews as hn
import scanners.world.reddit_ai as reddit_ai
from scanners import seen_items


# ---------------------------------------------------------------------------
# hackernews — gap 1: a persistent seen-set, with protect= actually load-bearing
# ---------------------------------------------------------------------------

def test_hackernews_a_pinned_front_page_story_never_re_fires(tmp_path, monkeypatch):
    """Without a persistent seen-set, a top-30 story that outlives the 6h
    habituation window re-fires every window, forever. With one — and with
    `protect=` guarding the current front page at the eviction cap — a story
    that stays pinned must never re-emit again after its cold-start baseline,
    even once ten sweeps' worth of newer ids would otherwise push its
    (permanently oldest, first-seen) stamp out of a small seen-set."""
    monkeypatch.setattr(hn, "_CACHE_PATH", tmp_path / "hackernews.json")
    monkeypatch.setattr(hn, "_SEEN_MAX", 5)  # small cap to force real eviction fast
    items: dict = {}

    def fake_fetch(url):
        if url == hn._TOP_URL:
            return list(items["_top"])
        sid = int(url.split("/item/")[1].split(".json")[0])
        return items[sid]

    monkeypatch.setattr(hn, "_fetch_json", fake_fetch)

    now = [1_000_000.0]
    monkeypatch.setattr(hn.time, "time", lambda: now[0])

    PINNED = 1
    items[PINNED] = {"title": "AI pinned story", "url": "", "score": 5, "time": now[0]}
    items[2] = {"title": "AI story 2", "url": "", "score": 5, "time": now[0]}
    items["_top"] = [PINNED, 2]

    assert hn.scan_hackernews() == []  # cold start: silent baseline, nothing emitted

    # Ten more sweeps, each with one brand-new story alongside the pinned one
    # — far more distinct ids than _SEEN_MAX=5, enough to force real eviction
    # of everything that is NOT protected.
    for i in range(3, 13):
        now[0] += hn._top_ttl() + 1
        items[i] = {"title": f"AI story {i}", "url": "", "score": 5, "time": now[0]}
        items["_top"] = [PINNED, i]
        out = hn.scan_hackernews()
        assert all(t["story_id"] != PINNED for t in out), (
            f"pinned story re-fired on sweep {i} -- its seen-set stamp was "
            "evicted, meaning protect= is not doing its job"
        )

    seen = json.loads(hn._seen_path().read_text())
    assert str(PINNED) in seen, "protect= must keep the pinned id alive past the cap"


# ---------------------------------------------------------------------------
# hackernews — gap 2: an emit cap, survivors sorted by urgency before the cut
# ---------------------------------------------------------------------------

def test_hackernews_caps_emissions_and_keeps_the_highest_urgency_ones(tmp_path, monkeypatch):
    """_MAX_STORIES (30) was the de-facto cap on one cycle -- nothing
    downstream bounded it, so HN could put 30 triggers in one sweep against
    ai_labs' 4 and github_issues' 5. _MAX_TRIGGERS caps it, and survivors are
    sorted by urgency DESCENDING before the cut, so the cap keeps the best
    candidates rather than whichever ones happened to sit first in HN's own
    top-list ranking."""
    monkeypatch.setattr(hn, "_CACHE_PATH", tmp_path / "hackernews.json")
    now = 1_000_000.0
    monkeypatch.setattr(hn.time, "time", lambda: now)
    # score -> urgency (age_h floors at 0.5h so urgency = 0.3 + score/200):
    # 1->0.305  2->0.3  no wait, see actual values inline below.
    scores = {1: 10, 2: 60, 3: 5, 4: 40, 5: 65, 6: 1}

    def fake_fetch(url):
        if url == hn._TOP_URL:
            return list(scores)
        sid = int(url.split("/item/")[1].split(".json")[0])
        return {"title": f"AI story {sid}", "url": "", "score": scores[sid], "time": now}

    monkeypatch.setattr(hn, "_fetch_json", fake_fetch)
    seen_items.write_seen(hn._seen_path(), {"__unrelated__": 0.0}, 10)  # not a cold start

    out = hn.scan_hackernews()

    assert hn._MAX_TRIGGERS == 4
    assert len(out) == 4
    assert [t["story_id"] for t in out] == [5, 2, 4, 1], (
        "must be the four HIGHEST-urgency stories (scores 65,60,40,10), in "
        "urgency order -- not the first four ids in the fake top list"
    )
    assert [t["urgency"] for t in out] == sorted((t["urgency"] for t in out), reverse=True)


# ---------------------------------------------------------------------------
# hackernews — gap 3: adopt safe_http.fetch_json
# ---------------------------------------------------------------------------

def test_hackernews_fetch_reuses_a_304_instead_of_going_blank(monkeypatch):
    """A bare urlopen() had no conditional GET at all. Routed through
    safe_http, a 304 (not_modified) must reuse the last successful body
    rather than the caller reading it as "no data"."""
    hn._HTTP_STATE.clear()
    calls: list = []

    def fake_fetch_json(url, *, state, headers=None, timeout=8.0, name=""):
        calls.append(url)
        if len(calls) == 1:
            return "ok", {"hits": 1}
        return "not_modified", None

    monkeypatch.setattr(hn.safe_http, "fetch_json", fake_fetch_json)

    first = hn._fetch_json("https://x/one")
    second = hn._fetch_json("https://x/one")

    assert first == {"hits": 1}
    assert second == {"hits": 1}, "a 304 must reuse the last 200's body"
    assert calls == ["https://x/one", "https://x/one"]


def test_hackernews_fetch_degrades_silently_on_a_rate_limit(monkeypatch):
    """A throttled HN used to read exactly like a dead feed (bare urlopen
    raised generically, no distinction). Routed through safe_http, a
    rate-limited fetch must degrade to None -- not raise, not crash the
    sweep."""
    hn._HTTP_STATE.clear()
    monkeypatch.setattr(hn.safe_http, "fetch_json",
                        lambda url, **kw: ("rate_limited", None))
    assert hn._fetch_json("https://x/two") is None


# ---------------------------------------------------------------------------
# reddit_ai — gap 4: per-sub last_fetched, dark feed costs one attempt per TTL
# ---------------------------------------------------------------------------

def test_reddit_ai_dark_feed_costs_one_fetch_attempt_per_sub_per_ttl_not_every_cycle(
    tmp_path, monkeypatch
):
    """The old cache was a bare list, rewritten only `if posts:` -- once
    every sub in a sweep failed, the file's mtime froze and the NEXT check
    read as permanently stale, so every subsequent ~7-minute cycle re-paid
    the full 30s x 2 stagger-sleep budget forever (measured against a 420s
    daemon interval). Per-sub `last_fetched`, persisted even on an all-dark
    sweep, must make the SECOND call inside the same TTL window skip every
    sub -- no fetch, no sleep."""
    monkeypatch.setenv("ZUGAMIND_DATA_DIR", str(tmp_path))
    fetch_calls: list = []
    sleep_calls: list = []

    def fake_fetch_sub(sub, state):
        fetch_calls.append(sub)
        return "failed", None

    monkeypatch.setattr(reddit_ai, "_fetch_sub", fake_fetch_sub)
    monkeypatch.setattr(reddit_ai.time, "sleep", lambda s: sleep_calls.append(s))
    monkeypatch.setattr(reddit_ai.time, "time", lambda: 1_000_000.0)

    assert reddit_ai.scan_reddit_ai() == []
    assert len(fetch_calls) == 3, "first sweep must still attempt every sub once"
    assert len(sleep_calls) == 2, "stagger sleep only between actual fetch attempts"

    cache_data = json.loads(reddit_ai._cache_path().read_text(encoding="utf-8"))
    for sub in reddit_ai._SUBS:
        assert cache_data["subs"][sub]["last_fetched"] == 1_000_000.0, (
            "the attempt must be persisted even though every sub failed"
        )

    fetch_calls.clear()
    sleep_calls.clear()
    # A later cycle inside the SAME TTL window (daemon cadence ~420s vs a
    # 3600s TTL) -- this is exactly the shape that used to re-pay the whole
    # sleep budget every single time.
    monkeypatch.setattr(reddit_ai.time, "time", lambda: 1_000_420.0)
    assert reddit_ai.scan_reddit_ai() == []
    assert fetch_calls == [], "a sub already attempted this window must not be re-fetched"
    assert sleep_calls == [], "and therefore never re-sleeps for it either"


# ---------------------------------------------------------------------------
# reddit_ai — gap 5: a malformed cached post must not enter the auction
# ---------------------------------------------------------------------------

def test_reddit_ai_skips_a_titleless_row_instead_of_emitting_r_question_mark(
    tmp_path, monkeypatch
):
    """A malformed cached post used to still emit a trigger with
    `detail='r/?: ?'` -- garbage entering the auction and burning a
    habituation slot."""
    monkeypatch.setenv("ZUGAMIND_DATA_DIR", str(tmp_path))
    now = time_module.time()
    state = {
        "subs": {
            "MachineLearning": {
                "last_fetched": now,
                "posts": [
                    {"sub": "MachineLearning", "title": "A real post",
                     "link": "https://x/1", "id": "good1"},
                    {"sub": "MachineLearning", "title": "", "link": "", "id": "bad1"},
                    {"sub": "MachineLearning"},  # missing title entirely
                ],
            },
            "LocalLLaMA": {"last_fetched": now, "posts": []},
            "singularity": {"last_fetched": now, "posts": []},
        }
    }
    reddit_ai._cache_path().write_text(json.dumps(state), encoding="utf-8")
    # Non-cold seen-set so this call emits directly instead of baselining.
    seen_items.write_seen(reddit_ai._seen_path(), {"__unrelated__": 0.0}, 10)

    triggers = reddit_ai.scan_reddit_ai()

    assert [t["detail"] for t in triggers] == ["r/MachineLearning: A real post"]
    assert not any("r/?: ?" in t["detail"] for t in triggers)


# ---------------------------------------------------------------------------
# reddit_ai — gap 6: adopt safe_http.fetch_json (the public JSON listing)
# ---------------------------------------------------------------------------

def test_reddit_ai_fetches_the_rss_feed_not_the_blocked_json_listing(monkeypatch):
    """The transport is .rss, and this test exists because swapping it cost
    nothing in CI and everything in production.

    reddit_ai briefly moved to Reddit's public .json listing in order to reach
    safe_http.fetch_json. Every test passed -- they all mock the fetch. Probed
    live 2026-08-29: the .json listing answers 403 Blocked for every
    User-Agent tried, while .rss returns 200 with eight entries. So the URL
    itself is now an assertion, and the RSS parse path is what gets covered.
    """
    atom = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        '<entry><title>A real\npost</title>'
        '<link href="https://www.reddit.com/r/singularity/comments/abc123/x/"/>'
        '<id>t3_abc123</id></entry>'
        '<entry><title>   </title><link href="https://example.com/y"/>'
        '<id>t3_def456</id></entry>'
        "</feed>"
    )
    seen_urls: list = []
    seen_headers: list = []

    def fake_fetch_text(url, *, state, headers=None, timeout=8.0, name=""):
        seen_urls.append(url)
        seen_headers.append(dict(headers or {}))
        return "ok", atom

    monkeypatch.setattr(reddit_ai.safe_http, "fetch_text", fake_fetch_text)
    status, posts = reddit_ai._fetch_sub("singularity", {})

    assert status == "ok"
    assert seen_urls == ["https://www.reddit.com/r/singularity/hot/.rss?limit=8"], \
        "the .json listing is 403 Blocked -- do not swap the transport back"
    # The UA is load-bearing too: the previous "ZugaMind/scanner" string 429s
    # on this same feed where this one gets a 200.
    assert seen_headers[0]["User-Agent"] == "ZugaMind/1.0 (read-only)"
    assert len(posts) == 1, "a blank-title entry must not become a post"
    assert posts[0]["id"] == "t3_abc123"
    assert posts[0]["title"] == "A real post", "newline collapsed to one space"


@pytest.mark.parametrize("body,why", [
    ("<html>not a feed</html>", "an error page served as 200 PARSES as XML"),
    ("<feed><entry>", "genuinely malformed"),
    ("", "empty body"),
])
def test_reddit_ai_a_wrong_document_is_a_failure_not_an_empty_sub(
        monkeypatch, body, why):
    """Reporting [] here lets the caller persist "this sub has nothing", which
    is how a reshaped feed goes quiet instead of loud. The first case is the
    sharp one: a Cloudflare interstitial or login wall is VALID XML, so a
    naive reader finds zero entries and calls it an empty subreddit."""
    monkeypatch.setattr(reddit_ai.safe_http, "fetch_text",
                        lambda url, **kw: ("ok", body))
    status, posts = reddit_ai._fetch_sub("singularity", {})
    assert status == "failed" and posts is None, why


def test_reddit_ai_a_genuinely_empty_atom_feed_is_still_ok(monkeypatch):
    """The mirror: a real feed with no entries must NOT read as a failure."""
    monkeypatch.setattr(
        reddit_ai.safe_http, "fetch_text",
        lambda url, **kw: ("ok", '<feed xmlns="http://www.w3.org/2005/Atom"/>'))
    status, posts = reddit_ai._fetch_sub("singularity", {})
    assert status == "ok" and posts == []

def test_hackernews_title_control_characters_and_bad_numbers_are_neutralized_before_detail(
    tmp_path, monkeypatch
):
    """`detail` is the literal briefing text handed to a paid model, so an
    embedded newline/control byte in a third-party title is a prompt-
    injection seam, not cosmetic noise (gap 7). And a non-numeric score/time
    must not raise mid-sweep or silently win the auction as NaN (gap 8) --
    this is the same invariant test_scanners_audit.py's
    test_urgency_cannot_leave_the_contract proves, re-run here on a WARM
    sweep since gap 1's cold-start baseline now eats that test's one sweep
    (see this file's module docstring)."""
    monkeypatch.setattr(hn, "_CACHE_PATH", tmp_path / "hackernews.json")
    monkeypatch.setattr(hn.time, "time", lambda: 1_000_000.0)
    nasty_title = "AI\n\tstartup   raises\x07 funding\x00 round"

    def fake_fetch(url):
        if url == hn._TOP_URL:
            return [42]
        return {"title": nasty_title, "url": "", "score": "not-a-number", "time": None}

    monkeypatch.setattr(hn, "_fetch_json", fake_fetch)
    seen_items.write_seen(hn._seen_path(), {"__unrelated__": 0.0}, 10)  # warm, not cold start

    out = hn.scan_hackernews()

    assert len(out) == 1
    trig = out[0]
    for ch in "\n\t\x07\x00":
        assert ch not in trig["detail"], f"{ch!r} leaked into detail: {trig['detail']!r}"
    assert "  " not in trig["detail"], "control-char collapse must not leave doubled spaces"
    for field in ("novelty", "relevance", "urgency"):
        assert 0.0 <= trig[field] <= 1.0, f"{field}={trig[field]}"


def test_reddit_ai_title_control_characters_do_not_reach_the_paid_model_briefing(
    tmp_path, monkeypatch
):
    """Same seam as the HN test above, on reddit_ai's `detail`. Seeded
    directly into the per-sub cache (rather than through _fetch_sub) so this
    also proves the cleaning happens again at the detail-construction site,
    not only on ingestion -- a pre-existing/legacy cache entry must not get
    a free pass."""
    monkeypatch.setenv("ZUGAMIND_DATA_DIR", str(tmp_path))
    now = time_module.time()
    nasty = "New paper\r\nbeats  SOTA\x0b on benchmark\x7f"
    state = {
        "subs": {
            "MachineLearning": {
                "last_fetched": now,
                "posts": [{"sub": "MachineLearning", "title": nasty,
                          "link": "https://x/9", "id": "p9"}],
            },
            "LocalLLaMA": {"last_fetched": now, "posts": []},
            "singularity": {"last_fetched": now, "posts": []},
        }
    }
    reddit_ai._cache_path().write_text(json.dumps(state), encoding="utf-8")
    seen_items.write_seen(reddit_ai._seen_path(), {"__unrelated__": 0.0}, 10)

    out = reddit_ai.scan_reddit_ai()

    assert len(out) == 1
    detail = out[0]["detail"]
    for ch in "\r\n\x0b\x7f":
        assert ch not in detail, f"{ch!r} leaked into detail: {detail!r}"
    assert "  " not in detail


def test_reddit_ai_emitted_scores_stay_within_the_0_1_contract(tmp_path, monkeypatch):
    """novelty/relevance/urgency are the workspace auction's currency --
    every emission site must be clamped, brand-mention path included, even
    though today's inputs are constants (defense in depth against the next
    edit that makes one of them a variable)."""
    monkeypatch.setenv("ZUGAMIND_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ZUGAMIND_BRAND_TERMS", "ZugaMind")
    now = time_module.time()
    state = {
        "subs": {
            "MachineLearning": {
                "last_fetched": now,
                "posts": [
                    {"sub": "MachineLearning", "title": "ZugaMind is interesting",
                     "link": "https://x/1", "id": "brand1"},
                    {"sub": "MachineLearning", "title": "Something else",
                     "link": "https://x/2", "id": "plain1"},
                ],
            },
            "LocalLLaMA": {"last_fetched": now, "posts": []},
            "singularity": {"last_fetched": now, "posts": []},
        }
    }
    reddit_ai._cache_path().write_text(json.dumps(state), encoding="utf-8")
    seen_items.write_seen(reddit_ai._seen_path(), {"__unrelated__": 0.0}, 10)

    out = reddit_ai.scan_reddit_ai()

    assert len(out) == 2
    for trig in out:
        for field in ("novelty", "relevance", "urgency"):
            assert 0.0 <= trig[field] <= 1.0, f"{field}={trig[field]}"
