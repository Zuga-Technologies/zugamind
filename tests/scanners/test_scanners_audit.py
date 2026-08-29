"""Regression tests for the 2026-08-29 scanners/ audit.

Named for the invariant, not the function. Every one of these fails against
the code as it stood that morning.
"""
from __future__ import annotations

import json
import logging
import tempfile
import urllib.request
from pathlib import Path

import pytest

import foundation.config as config
import scanners.scheduler as scheduler
import scanners.world.ai_labs as ai_labs
import scanners.world.hackernews as hackernews
from scanners import habituation_filter, safe_http, seen_items


@pytest.fixture
def tmp_seen(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SEEN_TRIGGERS_FILE", tmp_path / "seen.json")
    return tmp_path / "seen.json"


# ---------------------------------------------------------------------------
# safe_http — credentials must not cross an origin boundary
# ---------------------------------------------------------------------------

def _redirect(from_url: str, to_url: str) -> urllib.request.Request:
    handler = safe_http._CredentialStrippingRedirectHandler()
    req = urllib.request.Request(from_url)
    req.add_header("Authorization", "Bearer SENTINEL-NOT-A-REAL-TOKEN")
    req.add_header("User-Agent", "ZugaMind/test")
    return handler.redirect_request(req, None, 302, "Found", {}, to_url)


def test_credentials_are_stripped_when_a_redirect_leaves_the_origin():
    """urllib copies every header onto the redirect target with no host check,
    so a GitHub Bearer token was handed to whatever answered a 30x."""
    new = _redirect("https://api.github.com/repos/o/r/issues",
                    "https://evil.example.com/collect")
    assert new is not None
    assert "Authorization" not in new.headers
    assert new.headers.get("User-agent") == "ZugaMind/test", "only creds go"


def test_credentials_are_stripped_on_an_https_to_http_downgrade():
    new = _redirect("https://api.github.com/x", "http://api.github.com/x")
    assert "Authorization" not in new.headers


def test_credentials_survive_a_same_origin_redirect():
    """Stripping unconditionally would break ordinary API pagination."""
    new = _redirect("https://api.github.com/a", "https://api.github.com/b")
    assert new.headers.get("Authorization", "").startswith("Bearer ")


@pytest.mark.parametrize("raw,expected", [
    (-500000, 0.0), (2.5, 1.0), (float("nan"), 0.0), (float("inf"), 0.0),
    ("0.4", 0.4), (None, 0.0), (True, 0.0), (0.35, 0.35),
])
def test_scores_are_forced_into_the_0_1_contract(raw, expected):
    assert safe_http.clamp01(raw) == pytest.approx(expected)


def test_a_bom_does_not_lose_the_whole_response():
    assert json.loads(safe_http.decode_body(b"\xef\xbb\xbf{\"a\": 1}")) == {"a": 1}


# ---------------------------------------------------------------------------
# habituation — the docstring promises fail-OPEN; make it true
# ---------------------------------------------------------------------------

def test_a_future_timestamp_does_not_damp_forever(tmp_seen):
    """A clock artefact used to blind a trigger permanently AND survive every
    prune, in the one branch where this filter failed CLOSED."""
    tmp_seen.write_text(json.dumps({"hn_story:101": 1e6 + 3.15e8}))
    trigger = {"type": "hn_story", "story_id": 101, "detail": "x"}

    assert habituation_filter([trigger], now=1e6) == [trigger]
    assert json.loads(tmp_seen.read_text())["hn_story:101"] <= 1e6


def test_ids_the_world_scanners_actually_emit_are_used(tmp_seen):
    """post_slug/issue_id were missing from the id list, so reddit and
    github_issues keyed on a hash of the DETAIL TEXT: same-title posts
    collided, and a title edit re-fired."""
    post = lambda slug: {"type": "reddit_ai_post", "post_slug": slug,
                         "detail": "r/LocalLLaMA: Weekly discussion thread"}
    survivors = habituation_filter([post("t3_aaa"), post("t3_bbb")], now=1e6)
    assert len(survivors) == 2, "two different posts must not collide"

    issue = {"type": "repo_issue", "issue_id": 998877, "detail": "issue #12: Crash"}
    edited = dict(issue, detail="issue #12: Crash (macOS)")
    habituation_filter([issue], now=1e6)
    assert habituation_filter([edited], now=1e6 + 60) == [], "a title edit must not re-fire"


def test_an_unwritable_state_file_is_not_silent(tmp_path, monkeypatch, caplog):
    """Habituation goes permanently OFF, and used to say nothing at any level."""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    monkeypatch.setattr(config, "SEEN_TRIGGERS_FILE", blocker / "sub" / "s.json")
    with caplog.at_level(logging.WARNING, logger="zugamind.scanners"):
        habituation_filter([{"type": "t", "id": "1", "detail": "d"}], now=1e6)
    assert any("damping is OFF" in r.message for r in caplog.records)


def test_one_malformed_trigger_does_not_abandon_the_batch(tmp_seen):
    """A non-dict raised mid-loop, discarding the bookkeeping for every
    well-formed trigger already processed -- so the batch re-fired forever."""
    good = {"type": "a", "id": "1", "detail": "d"}
    other = {"type": "a", "id": "2", "detail": "d2"}
    assert habituation_filter([good, "i am a string", other], now=1e6) == [good, other]
    assert habituation_filter([good], now=1e6 + 60) == [], "the good one was recorded"


def test_a_zero_habituation_window_is_announced(tmp_seen, monkeypatch, caplog):
    """One character in an env var turns the agent into the cron job the
    README says it is not."""
    monkeypatch.setattr(config, "HABITUATION_HOURS", 0)
    with caplog.at_level(logging.WARNING, logger="zugamind.scanners"):
        habituation_filter([{"type": "t", "id": "1", "detail": "d"}], now=1e6)
    assert any("damping is OFF" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# seen_items — the pinned-post eviction
# ---------------------------------------------------------------------------

def test_an_item_still_on_the_feed_is_never_evicted(tmp_path):
    """The stored stamp is FIRST-seen and never refreshed, so a stickied post
    holds the oldest stamp and was the first thing dropped at the cap -- then
    it re-fired. Exactly the bug the module exists to prevent."""
    path = tmp_path / "seen.json"
    seen = {"PINNED": 1000.0}
    seen.update({f"gone_{i}": 2000.0 + i for i in range(5)})
    seen_items.write_seen(path, seen, max_keys=5, protect={"PINNED"})
    assert "PINNED" in json.loads(path.read_text())


def test_a_zero_cap_refuses_to_wipe_the_set(tmp_path, caplog):
    path = tmp_path / "seen.json"
    with caplog.at_level(logging.WARNING, logger="zugamind.scanners.seen_items"):
        seen_items.write_seen(path, {"a": 1.0}, max_keys=0)
    assert not path.exists(), "writing {} here re-fires the entire feed"


def test_one_bad_value_does_not_discard_the_whole_set(tmp_path):
    """A single null used to raise inside a comprehension and throw away
    hundreds of remembered items, which reads to the caller as a cold start."""
    path = tmp_path / "seen.json"
    data = {f"k{i}": float(i) for i in range(50)}
    data["bad"] = None
    path.write_text(json.dumps(data))
    loaded = seen_items.read_seen(path)
    assert loaded is not None and len(loaded) == 50 and "bad" not in loaded


# ---------------------------------------------------------------------------
# scheduler — a clock artefact must not blind a source
# ---------------------------------------------------------------------------

def test_a_future_poll_stamp_is_treated_as_due(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("ZUGAMIND_SOURCE_SCHEDULER_ENABLED", "1")
    monkeypatch.setattr(scheduler, "_LEDGER_PATH", tmp_path / "ledger.json")
    sched = scheduler.SourceScheduler(
        specs={"hn": scheduler.SourceSpec("hn", base_cadence_secs=300)})
    sched.record_yield("hn", 1, now=1e6)
    with caplog.at_level(logging.WARNING, logger=scheduler.logger.name):
        assert sched.due("hn", 1e6 - 3600) is True
    assert any("FUTURE" in r.message for r in caplog.records)


@pytest.mark.parametrize("value", ["", "   ", "3.5", "not-a-number"])
def test_a_mistyped_dial_degrades_instead_of_killing_the_process(monkeypatch, value):
    """These were bare int() at module level, and the runner imports this at
    module scope -- so one bad character stopped the agent from booting."""
    monkeypatch.setenv("ZUGAMIND_PER_SCANNER_CAP", value)
    assert scheduler._default_emit_cap() == 3


# ---------------------------------------------------------------------------
# hackernews
# ---------------------------------------------------------------------------

def test_urgency_cannot_leave_the_contract(monkeypatch, tmp_path):
    """urgency is auction currency: an out-of-range value does not look
    wrong, it outbids every honest sense. Measured -2499.7 before the clamp."""
    monkeypatch.setattr(hackernews, "_CACHE_PATH", tmp_path / "hn.json")
    monkeypatch.setattr(hackernews, "_fetch_json", lambda url: (
        [1] if url == hackernews._TOP_URL
        else {"title": "AI thing", "url": "", "score": -500000, "time": 1e9}))
    monkeypatch.setattr(hackernews.time, "time", lambda: 1e9 + 3600)
    for trigger in hackernews.scan_hackernews():
        for field in ("novelty", "relevance", "urgency"):
            assert 0.0 <= trigger[field] <= 1.0, f"{field}={trigger[field]}"


@pytest.mark.parametrize("title", [
    "Fine-tuning our stack", "fine-tune the pipeline", "Fine-tuned results",
    "Open-Sourcing our compiler", "They open-sourced the model",
])
def test_the_keep_filter_matches_the_words_people_actually_write(title):
    """`fine-tun` and `open[- ]?source` sat inside \\b(...)\\b, so the trailing
    boundary made them match nothing. A dead branch in a KEEP filter is
    silent -- it just stops surfacing real stories."""
    assert hackernews._KEEP_RE.search(title), title


# ---------------------------------------------------------------------------
# ai_labs classification
# ---------------------------------------------------------------------------

def test_a_lab_crediting_its_own_supplier_is_still_a_launch():
    """'Powered by Cerebras' in the body is a hardware credit under the lab's
    OWN product. The bare-phrase rule scored it under the wake floor."""
    assert ai_labs._relevance_for(
        "Previewing Ultrafast mode: GPT-5.6 Sol at up to 14X the speed",
        "Preview Ultrafast, a new OpenAI API service tier that runs GPT-5.6 "
        "Sol up to 14x faster. Powered by Cerebras.",
        "openai",
    ) == ai_labs._RELEVANCE_HIGH


def test_a_partner_running_the_labs_model_is_still_promotion():
    """The other direction, and the reason precedence was not the fix: the
    2026-08-19 Replit wake must stay demoted."""
    assert ai_labs._relevance_for(
        "Replit expands access to software creation with GPT-5.6 Luna",
        "Replit introduces Free Mode, powered by GPT-5.6 Luna, so anyone can "
        "turn ideas into working software.",
    ) == ai_labs._RELEVANCE_NON_WORK


@pytest.mark.parametrize("title", [
    "How loveholidays is making everyone a builder with Codex",
    "How Shopify is transforming support with Claude",
])
def test_customer_case_studies_do_not_price_like_work(title):
    """A lowercase brand or an auxiliary+participle walked past the verb
    whitelist and priced above the wake floor."""
    assert ai_labs._relevance_for(title) == ai_labs._RELEVANCE_NON_WORK


@pytest.mark.parametrize("title", [
    "How we built a realtime system for voice",
    "How Claude's text watermark works",
    "How OpenAI delivers low-latency voice AI at scale",
])
def test_first_person_and_first_party_how_posts_are_protected(title):
    """The case-study rule must not eat real engineering write-ups."""
    assert ai_labs._relevance_for(title) == ai_labs._RELEVANCE_DEFAULT


@pytest.mark.parametrize("title", [
    "OpenAI partners with Scale to provide support for enterprises",
    "Google DeepMind partners with game studios",
    "Our agreement with the Department of War",
])
def test_the_verb_form_of_a_partnership_is_public_affairs_too(title):
    """The noun forms were covered; the verb form is how the announcements
    are actually titled."""
    assert ai_labs._relevance_for(title) == ai_labs._RELEVANCE_NON_WORK
