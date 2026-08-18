"""ai_labs: an item triggers once, and its numbers are evidence.

Regression suite for the 2026-08-18 finding — "Introducing Claude Opus 5"
(published Jul 24) won the global workspace eight times over twelve days and
bought a harness wake, because the scanner had no per-item dedupe and shipped
a hardcoded urgency. Both are asserted here.

Extended the same evening: relevance was hardcoded too, which put every fresh
lab post at exactly the 0.600 wake floor and let an OpenAI national-security
policy announcement buy a Claude session at 20:11Z.
"""
from __future__ import annotations

import json
import time

import pytest

import scanners.world.ai_labs as ai_labs


HOUR = 3600.0
DAY = 24 * HOUR


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "scanner_cache"
    d.mkdir(parents=True)
    monkeypatch.setattr(ai_labs, "_CACHE_DIR", d)
    return d


def _seed(items, ts=None):
    ai_labs._write_cache({"ts": ts if ts is not None else time.time(), "items": items})


def _item(lab="anthropic", slug="a", title="A post", published=None, summary=""):
    return {"lab": lab, "title": title, "summary": summary,
            "link": f"https://example.invalid/{lab}/{slug}", "published": published}


# --------------------------------------------------------------------------
# test isolation — the cache used to be bound at import, so patching
# _CACHE_DIR (all tests/conftest.py does) left writes hitting the real tree
# --------------------------------------------------------------------------

def test_cache_and_seen_files_follow_patched_cache_dir(cache_dir):
    _seed([_item(published=time.time())])
    ai_labs.scan_ai_labs()
    assert (cache_dir / "ai_labs.json").exists()
    assert (cache_dir / "ai_labs_seen.json").exists()


# --------------------------------------------------------------------------
# dedupe
# --------------------------------------------------------------------------

def test_cold_start_baselines_silently(cache_dir):
    """Deploying the seen-set must not read the back catalogue as news."""
    _seed([_item(slug=str(i), published=time.time()) for i in range(6)])

    assert ai_labs.scan_ai_labs() == []

    seen = json.loads((cache_dir / "ai_labs_seen.json").read_text())
    assert len(seen) == 6


def test_item_triggers_once_and_never_again(cache_dir):
    _seed([_item(slug="old")])
    ai_labs.scan_ai_labs()  # cold-start baseline

    _seed([_item(slug="old"), _item(slug="new", title="New post", published=time.time())])
    first = ai_labs.scan_ai_labs()
    assert [t["title"] for t in first] == ["New post"]

    assert ai_labs.scan_ai_labs() == []
    assert ai_labs.scan_ai_labs() == []


def test_pinned_stale_item_cannot_refire_after_habituation(cache_dir):
    """The actual Opus-5 shape: one item sits at the top of the feed forever.

    Engine habituation would have let it back through after six hours; the
    seen-set is what makes "already emitted" permanent.
    """
    opus = _item(slug="claude-opus-5", title="Introducing Claude Opus 5",
                 published=time.time() - 25 * DAY)
    _seed([opus])
    ai_labs.scan_ai_labs()

    _seed([opus, _item(slug="fresh", title="Fresh", published=time.time())])
    assert [t["title"] for t in ai_labs.scan_ai_labs()] == ["Fresh"]

    for _ in range(10):
        _seed([opus, _item(slug="fresh", title="Fresh", published=time.time())])
        assert ai_labs.scan_ai_labs() == []


def test_seen_set_prunes_by_recency_not_key_order(cache_dir, monkeypatch):
    monkeypatch.setattr(ai_labs, "_SEEN_MAX", 3)
    now = time.time()
    ai_labs._write_seen({"zzz-newest": now, "aaa-oldest": now - 100, "mmm": now - 50,
                         "bbb-older": now - 90})

    seen = json.loads((cache_dir / "ai_labs_seen.json").read_text())
    assert set(seen) == {"zzz-newest", "mmm", "bbb-older"}


# --------------------------------------------------------------------------
# urgency is evidence
# --------------------------------------------------------------------------

@pytest.mark.parametrize("age_h,expected", [
    (0.0, 0.25),
    (23.0, 0.25),
    (48.0, 0.125),
    (72.0, 0.0),
    (25 * 24, 0.0),
])
def test_urgency_decays_with_publish_age(age_h, expected):
    now = time.time()
    assert ai_labs._urgency_for(now - age_h * HOUR, now) == pytest.approx(expected)


def test_unknown_publish_date_fails_open(cache_dir):
    """A parser regression must not silently make the scanner deaf."""
    now = time.time()
    assert ai_labs._urgency_for(None, now) == 0.25

    _seed([_item(slug="baseline")])
    ai_labs.scan_ai_labs()
    _seed([_item(slug="baseline"), _item(slug="undated", title="Undated")])
    assert ai_labs.scan_ai_labs()[0]["urgency"] == 0.25


def test_stale_item_bids_below_a_fresh_one(cache_dir):
    """WorldSignals prices 0.25 + 0.4*relevance + 0.2*urgency. At the
    operational relevance of 0.75, urgency is what separates a month-old post
    from this morning's — 0.60 (at the default 0.600 floor) vs 0.55."""
    now = time.time()
    _seed([_item(slug="baseline")])
    ai_labs.scan_ai_labs()

    _seed([_item(slug="baseline"),
           _item(lab="openai", slug="fresh", title="Fresh", published=now - HOUR),
           _item(lab="deepmind", slug="stale", title="Stale", published=now - 25 * DAY)])
    by_title = {t["title"]: t for t in ai_labs.scan_ai_labs()}

    def salience(t):
        return min(0.75, 0.25 + 0.4 * t["relevance"] + 0.2 * t["urgency"])

    assert salience(by_title["Fresh"]) == pytest.approx(0.60)
    assert salience(by_title["Stale"]) == pytest.approx(0.55)


def test_newest_first_within_a_lab(cache_dir):
    """anthropic.com/news leads with a pinned block, so feed order put a
    month-old card first and the newest post fifth, past _MAX_TRIGGERS."""
    now = time.time()
    _seed([_item(slug="baseline")])
    ai_labs.scan_ai_labs()

    _seed([_item(slug="baseline")] + [
        _item(slug="pinned", title="Pinned old", published=now - 25 * DAY),
        _item(slug="mid", title="Middle", published=now - 5 * DAY),
        _item(slug="newest", title="Newest", published=now - HOUR),
    ])
    assert [t["title"] for t in ai_labs.scan_ai_labs()][0] == "Newest"


# --------------------------------------------------------------------------
# relevance is evidence
# --------------------------------------------------------------------------

def _salience(relevance, urgency):
    """WorldSignalsModule.generate_bid, verbatim."""
    return min(0.75, 0.25 + 0.4 * relevance + 0.2 * urgency)


@pytest.mark.parametrize("title", [
    # the post that woke a session on 2026-08-18 at 20:11Z, and its siblings
    "Strengthening Democratic Oversight in National Security",
    "New policy ideas for the Intelligence Age",
    "Our comment on the EU AI Act",
    "Working with the U.S. Congress on AI",
    "Tino Cuellar to join Anthropic as Chief Global Affairs Officer",
    "Findings from the Anthropic Economic Index",
])
def test_public_affairs_posts_price_below_the_wake_floor(title):
    relevance = ai_labs._relevance_for(title)
    assert relevance == ai_labs._RELEVANCE_PUBLIC_AFFAIRS
    assert _salience(relevance, 0.25) < 0.600


@pytest.mark.parametrize("title", [
    "Introducing Claude Opus 5",
    "How Claude's text watermark works",
    "Improving Fable 5's biology safeguards",
    "Investigating three real-world incidents in our cybersecurity evaluations",
    # terms with a technical reading must NOT read as public affairs
    "A new policy gradient method for agent training",
    "AI governance for model deployment",
    "Echoverse: evolving environments for computer-use agents",
])
def test_work_posts_keep_the_operational_relevance(title):
    assert ai_labs._relevance_for(title) == ai_labs._RELEVANCE_DEFAULT


def test_unrecognized_subject_fails_open(cache_dir):
    """A vocabulary that drifts out of date must not make the scanner deaf."""
    assert ai_labs._relevance_for("", "") == ai_labs._RELEVANCE_DEFAULT
    assert ai_labs._relevance_for("Qwertyuiop asdfghjkl") == ai_labs._RELEVANCE_DEFAULT

    _seed([_item(slug="baseline")])
    ai_labs.scan_ai_labs()
    _seed([_item(slug="baseline"), _item(slug="odd", title="Zxcvbnm")])
    assert ai_labs.scan_ai_labs()[0]["relevance"] == ai_labs._RELEVANCE_DEFAULT


def test_the_summary_counts_not_just_the_title(cache_dir):
    """openai's RSS carries the subject in the description, not the headline."""
    assert ai_labs._relevance_for(
        "The Defender's Window",
        "What this means for lawmakers drafting the next AI act.",
    ) == ai_labs._RELEVANCE_PUBLIC_AFFAIRS


def test_a_fresh_policy_post_no_longer_clears_the_floor(cache_dir):
    """The 2026-08-18 20:11Z wake, replayed end to end.

    A fresh lab post used to price at exactly 0.600 whatever its subject, and
    the gate filters on `salience < floor` — so every one of them cleared the
    0.600 floor by a margin of zero.
    """
    now = time.time()
    _seed([_item(slug="baseline")])
    ai_labs.scan_ai_labs()

    _seed([_item(slug="baseline"),
           _item(lab="openai", slug="policy", published=now - HOUR,
                 title="Strengthening Democratic Oversight in National Security"),
           _item(lab="anthropic", slug="model", published=now - HOUR,
                 title="Introducing Claude Opus 5")])
    by_title = {t["title"]: t for t in ai_labs.scan_ai_labs()}

    policy = by_title["Strengthening Democratic Oversight in National Security"]
    model = by_title["Introducing Claude Opus 5"]
    assert policy["urgency"] == model["urgency"] == 0.25  # equally fresh
    assert _salience(policy["relevance"], policy["urgency"]) == pytest.approx(0.46)
    assert _salience(model["relevance"], model["urgency"]) == pytest.approx(0.60)


# --------------------------------------------------------------------------
# date extraction
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,ymd", [
    # real anthropic card shapes — the date renders before or after the label
    ("Introducing Claude Opus 5 Product Jul 24, 2026 Opus 5 is a step change", (2026, 7, 24)),
    ("Aug 14, 2026 Announcements How Claude's text watermark works", (2026, 8, 14)),
    ("Alignment May 8, 2026 Teaching Claude why", (2026, 5, 8)),
    # RFC 822 (RSS) and ISO 8601 (Atom / JSON)
    ("Tue, 14 Aug 2026 09:00:00 +0000", (2026, 8, 14)),
    ("2026-08-14T09:00:00Z", (2026, 8, 14)),
])
def test_parse_date_handles_every_shipped_feed_shape(text, ymd):
    import datetime

    ts = ai_labs._parse_date(text)
    assert ts is not None
    got = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
    assert (got.year, got.month, got.day) == ymd


@pytest.mark.parametrize("text", ["", "no date at all", "Foo 99, 2026", None])
def test_parse_date_returns_none_rather_than_guessing(text):
    assert ai_labs._parse_date(text) is None


def test_rss_parser_captures_pubdate():
    xml = """<rss><channel><item>
      <title>A model release</title>
      <link>https://example.invalid/post</link>
      <description>Body text</description>
      <pubDate>Tue, 14 Aug 2026 09:00:00 +0000</pubDate>
    </item></channel></rss>"""
    items = ai_labs._parse_feed(xml, "openai")
    assert len(items) == 1
    assert items[0]["published"] == ai_labs._parse_date("Tue, 14 Aug 2026 09:00:00 +0000")


def test_anthropic_scraper_captures_card_date():
    html = ('<a href="/news/claude-opus-5">Product Jul 24, 2026 '
            'Introducing Claude Opus 5</a>')
    items = ai_labs._parse_anthropic_html(html, "anthropic")
    assert len(items) == 1
    assert items[0]["published"] == ai_labs._parse_date("Jul 24, 2026")


def test_hf_json_captures_published_at():
    payload = json.dumps([{"paper": {"id": "2608.15089", "title": "A paper",
                                     "summary": "s", "publishedAt": "2026-08-14T09:00:00Z"}}])
    items = ai_labs._parse_hf_json(payload)
    assert len(items) == 1
    assert items[0]["published"] == ai_labs._parse_date("2026-08-14T09:00:00Z")


# --------------------------------------------------------------------------
# scanner contract
# --------------------------------------------------------------------------

def test_unreadable_seen_file_is_treated_as_cold_start(cache_dir):
    (cache_dir / "ai_labs_seen.json").write_text("{ not json")
    _seed([_item(published=time.time())])

    assert ai_labs.scan_ai_labs() == []
    assert json.loads((cache_dir / "ai_labs_seen.json").read_text())


def test_every_feed_down_stays_cold_instead_of_baselining_empty(cache_dir):
    """Baselining an empty sweep would mark nothing seen, then read the whole
    back catalogue as new on the next sweep that actually reaches a feed."""
    _seed([])

    assert ai_labs.scan_ai_labs() == []
    assert not (cache_dir / "ai_labs_seen.json").exists()

    _seed([_item(slug=str(i), published=time.time()) for i in range(4)])
    assert ai_labs.scan_ai_labs() == []  # cold start happens now, silently
