"""Regression tests for the 2026-08-29 ai_labs audit (a follow-up pass after
the classification fixes already landed the same day -- the "powered by"
object rule, the case-study grammar, and the partners-with verb form).

Seven gaps, each proved against a live fetch or the live cache before being
fixed here:

  1. SILENT DEAFNESS. A 200 whose body doesn't parse used to replace a feed's
     cached items with [] and store the error page's own ETag, so a later 304
     against that ETag re-served the emptiness forever -- with nothing logged
     at any level. `_read_cache` also read with no explicit encoding while
     `read_seen` (scanners.seen_items) already reads utf-8.
  2. `_HIGH_RELEVANCE_RE`'s model-token half required a digit IMMEDIATELY
     after the family name, so real product lines with a qualifier word
     ("Gemini Omni 1.1 Flash", "Gemini Robotics ER 2") missed HIGH.
  3. The cold-start guard (`if not items`) only fires once, for whichever
     feeds happened to answer on the very first sweep. A feed that has been
     down since deployment and only later starts answering dumped its whole
     back catalogue as breaking news the moment it recovered.
  4. `_parse_anthropic_html` put the WHOLE CARD -- category, date, headline
     AND body blurb, up to 200 chars -- into `title`, breaking the documented
     "HIGH is matched against the title alone" invariant and putting raw HTML
     entities (&#x27;) and a mid-word cut in front of the human.
  5. `_SCI_DOMAIN_RE` (AI applied to a field that isn't the models) was
     missing health/clinical/biomedical and weather/climate/Earth-system,
     leaving several live cached items at DEFAULT.
  6. The sort key was `it.get("published") or 0.0`, so an item whose date
     didn't parse sorted BEHIND a 700-hour-old dated one -- the opposite of
     the file's own "unknown age fails OPEN" doctrine.
  7. Every parse failure was a bare `except: return []` with no log; every
     fetch failure logged at debug only, forever -- indistinguishable from a
     feed that failed once from one that has been dark for a week.

Per the task's own method: gaps 2 and 5 are checked against the real fetched
corpus at zugamind/data/scanner_cache/ai_labs.json (see the module comments
above _HIGH_RELEVANCE_RE / _SCI_DOMAIN_RE for the exact before/after diff),
and the relevance/parsing tests below use text copied VERBATIM from that
cache file and from a live fetch of anthropic.com/news, closing the blind
spot the audit named: no existing test used a real fetched card or a real
fetched relevance corpus item, only synthetic titles.
"""
from __future__ import annotations

import json
import logging
import time

import pytest

import scanners.world.ai_labs as ai_labs


HOUR = 3600.0


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "scanner_cache"
    d.mkdir(parents=True)
    monkeypatch.setattr(ai_labs, "_CACHE_DIR", d)
    return d


def _item(lab="anthropic", slug="a", title="A post", published=None, summary=""):
    return {"lab": lab, "title": title, "summary": summary,
            "link": f"https://example.invalid/{lab}/{slug}", "published": published}


def _seed(items, ts=None):
    ai_labs._write_cache({"ts": ts if ts is not None else time.time(), "items": items})


# ---------------------------------------------------------------------------
# gap 1 -- silent deafness
# ---------------------------------------------------------------------------

def test_a_200_that_parses_to_nothing_keeps_the_old_items_and_warns(cache_dir, monkeypatch, caplog):
    """The exact bug: a 200 whose body doesn't parse used to overwrite the
    feed's cache with [] and store the broken page's OWN ETag, so a later 304
    against that ETag would reuse the empty list forever -- silently. Fixed:
    keep the last known-good items AND validators, so the next sweep still
    attempts a real GET, and warn every time this happens."""
    _, url, _fmt = next(f for f in ai_labs._FEEDS if f[0] == "openai")
    kept = [_item(lab="openai", slug="kept", title="Kept item")]
    ai_labs._write_cache({"ts": 0, "items": kept,
                          "feeds": {url: {"validators": {"etag": '"old-good-etag"'}, "items": kept}}})

    def fetch_broken_page(u, validators=None):
        if u == url:
            # A real Cloudflare interstitial doesn't contain <item>/<entry>
            # tags -- this is a well-formed XML document that just isn't RSS.
            return ("ok", "<html>please enable javascript</html>", {"etag": '"broken-page-etag"'})
        return ("failed", None, {})
    monkeypatch.setattr(ai_labs, "_fetch", fetch_broken_page)

    with caplog.at_level(logging.WARNING, logger="zugamind.scanners.ai_labs"):
        ai_labs.scan_ai_labs()
        assert any("parsed to zero items" in r.message for r in caplog.records)

    cache = json.loads(ai_labs._cache_file().read_text(encoding="utf-8"))
    assert [i["title"] for i in cache["feeds"][url]["items"]] == ["Kept item"]
    # The OLD validators survive, not the broken page's -- sending the broken
    # page's own ETag back would just buy a 304 against the same emptiness.
    assert cache["feeds"][url]["validators"] == {"etag": '"old-good-etag"'}


def test_read_cache_uses_explicit_utf8_encoding(cache_dir):
    """`_read_cache` used `path.read_text()` with no encoding while
    `read_seen` (scanners.seen_items) already reads utf-8 explicitly. On a
    platform whose default encoding is not utf-8, a non-Latin-1 character in
    a cached title decodes to mojibake instead of round-tripping."""
    title = "深層学習の新しいモデル"  # a title outside the Latin-1 range
    ai_labs._cache_file().write_bytes(
        json.dumps({"ts": time.time(), "items": [_item(title=title)]}).encode("utf-8")
    )
    cache = ai_labs._read_cache()
    assert cache["items"][0]["title"] == title


# ---------------------------------------------------------------------------
# gap 2 -- a qualifier word between the family name and the version
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    # both copied verbatim from zugamind/data/scanner_cache/ai_labs.json
    # (lab "deepmind"). Verified against the live cache with:
    #   python -c "import sys;sys.path.insert(0,'zugamind');import
    #   scanners.world.ai_labs as A;print([A._relevance_for(t) for t in (...)])"
    # -- both sat at DEFAULT (0.75) before this fix, HIGH (0.95) after, and
    # re-running the new pattern over the full 56-item live cache changes
    # only these two items' class (zero collateral).
    "Gemini Omni 1.1 Flash lets you build with more control",
    "Gemini Robotics ER 2: powering robotics with video understanding, "
    "task orchestration, and multi-robot collaboration",
])
def test_a_real_product_line_with_a_qualifier_word_reaches_high(title):
    assert ai_labs._relevance_for(title) == ai_labs._RELEVANCE_HIGH


@pytest.mark.parametrize("title", [
    # An ordinary sentence connector before an unrelated number must NOT
    # promote -- the fix widens the pattern to a small whitelist of known
    # lineup/mode words (omni, robotics, er, flash, pro, ...), not "any word",
    # specifically because an "any capitalized word" version matched this
    # shape (title-case headlines, not sentence-case, put ordinary words in
    # capitals too).
    "Gemini across 12 benchmarks",
    "New Sonnet Update Adds 2 Features",
    "Compares GPT and Claude across 5 languages",
])
def test_an_ordinary_word_before_a_digit_does_not_reach_high(title):
    assert ai_labs._relevance_for(title) != ai_labs._RELEVANCE_HIGH


# ---------------------------------------------------------------------------
# gap 3 -- the cold-start baseline is per feed, not global
# ---------------------------------------------------------------------------

def test_a_feed_recovering_after_others_are_established_is_baselined_not_flooded(cache_dir, monkeypatch):
    """The exact bug: on a first sweep where only 1 of 7 feeds answers, the
    seen-set's cold start baselines ONLY that feed (the global "seen is None"
    branch only ever fires once). When a second feed finally answers -- even
    weeks later -- its whole back catalogue used to read as breaking news.
    Replayed here as three sweeps: (1) only anthropic answers -- global cold
    start, silent; (2) openai answers for the first time with three
    back-catalogue-shaped items -- must be silently baselined too, not
    flooded; (3) openai publishes one truly new post -- that one, and only
    that one, must fire."""
    monkeypatch.setattr(ai_labs, "_CACHE_TTL", -1)  # every sweep re-fetches
    _, anthropic_url, _fmt = ai_labs._FEEDS[0]
    _, openai_url, _fmt2 = next(f for f in ai_labs._FEEDS if f[0] == "openai")

    def rss(*titles):
        body = "".join(
            f"<item><title>{t}</title><link>https://x.invalid/{t}</link>"
            f"<pubDate>Tue, 14 Aug 2026 09:00:00 +0000</pubDate></item>"
            for t in titles
        )
        return f"<rss><channel>{body}</channel></rss>"

    def sweep_1_only_anthropic(url, validators=None):
        if url == anthropic_url:
            return ("ok", '<a href="/news/first">Product Jul 1, 2026 First post here</a>', {})
        return ("failed", None, {})
    monkeypatch.setattr(ai_labs, "_fetch", sweep_1_only_anthropic)
    assert ai_labs.scan_ai_labs() == []

    def sweep_2_openai_first_answer(url, validators=None):
        if url == openai_url:
            return ("ok", rss("Old post A", "Old post B", "Old post C"), {})
        return ("failed", None, {})
    monkeypatch.setattr(ai_labs, "_fetch", sweep_2_openai_first_answer)
    assert ai_labs.scan_ai_labs() == []  # openai's whole first batch: silent

    def sweep_3_openai_one_new_post(url, validators=None):
        if url == openai_url:
            return ("ok", rss("Old post A", "Old post B", "Old post C", "Brand new post"), {})
        return ("failed", None, {})
    monkeypatch.setattr(ai_labs, "_fetch", sweep_3_openai_one_new_post)
    titles = [t["title"] for t in ai_labs.scan_ai_labs()]
    assert titles == ["Brand new post"]


# ---------------------------------------------------------------------------
# gap 4 -- the anthropic scraper reads the headline, not the whole card
# ---------------------------------------------------------------------------

# Fetched verbatim from https://www.anthropic.com/news on 2026-08-29 (the <a>
# wrapper's own class list is trimmed since the parser only checks for
# href="..." -- everything from the href onward is the real response body).

_REAL_HERO_CARD = (
    '<a href="/news/model-hardware-standard-research-preview">'
    '<h2 class="headline-4 FeaturedGrid-module-scss-module__W1FydW__featuredTitle">'
    'Previewing the Model Hardware Standard</h2>'
    '<div class="FeaturedGrid-module-scss-module__W1FydW__featuredItemContent">'
    '<div class="FeaturedGrid-module-scss-module__W1FydW__gridItem '
    'FeaturedGrid-module-scss-module__W1FydW__featured">'
    '<div class="FeaturedGrid-module-scss-module__W1FydW__meta">'
    '<span class="caption bold">Announcements</span>'
    '<time class="FeaturedGrid-module-scss-module__W1FydW__date caption bold">Aug 27, 2026</time>'
    '</div>'
    '<p class="body-3 serif FeaturedGrid-module-scss-module__W1FydW__body">'
    'We’re opening a research preview of the Model Hardware Standard (MHS), '
    'a shared specification for AI agents to safely operate physical devices, '
    'to a first group of scientific research labs and advanced manufacturers. '
    '</p></div></div></a>'
)

_REAL_ENTITY_CARD = (
    '<a href="/news/improving-fable-5-s-biology-safeguards">'
    '<div class="PublicationList-module-scss-module__KxYrHG__meta">'
    '<time class="PublicationList-module-scss-module__KxYrHG__date body-3">Aug 7, 2026</time>'
    '<span class="PublicationList-module-scss-module__KxYrHG__subject body-3">Product</span>'
    '</div>'
    '<span class="PublicationList-module-scss-module__KxYrHG__title body-3">'
    'Improving Fable 5&#x27;s biology safeguards</span></a>'
)

_REAL_PUBLIC_AFFAIRS_CARD = (
    '<a href="/news/tino-cuellar">'
    '<div class="PublicationList-module-scss-module__KxYrHG__meta">'
    '<time class="PublicationList-module-scss-module__KxYrHG__date body-3">Aug 4, 2026</time>'
    '<span class="PublicationList-module-scss-module__KxYrHG__subject body-3">Announcements</span>'
    '</div>'
    '<span class="PublicationList-module-scss-module__KxYrHG__title body-3">'
    'Mariano-Florentino (Tino) Cuéllar to join Anthropic as Chief Global '
    'Affairs Officer</span></a>'
)


def test_a_real_featured_card_yields_headline_only_not_the_whole_card():
    """The headline invariant: HIGH is matched against the title alone. Before
    this fix, `title` for this card would have been the category label, the
    date, AND the body blurb concatenated -- so a blurb mentioning "API" or a
    model version could have promoted a card whose actual headline doesn't."""
    items = ai_labs._parse_anthropic_html(_REAL_HERO_CARD, "anthropic")
    assert len(items) == 1
    assert items[0]["title"] == "Previewing the Model Hardware Standard"
    assert items[0]["summary"].startswith("We’re opening a research preview")
    assert "Announcements" not in items[0]["title"]
    assert "Aug 27, 2026" not in items[0]["title"]


def test_a_real_card_with_an_html_entity_is_unescaped():
    """`&#x27;` is how anthropic's own markup renders an apostrophe. Before
    this fix it reached the human in `detail` verbatim as "Fable 5&#x27;s"."""
    items = ai_labs._parse_anthropic_html(_REAL_ENTITY_CARD, "anthropic")
    assert len(items) == 1
    assert items[0]["title"] == "Improving Fable 5's biology safeguards"
    assert "&#x27;" not in items[0]["title"]


def test_a_real_publicationlist_card_has_no_body_blurb():
    """This template has no <p class="...body..."> at all -- summary must
    come back empty rather than picking up a neighboring card's blurb or the
    date/category text."""
    items = ai_labs._parse_anthropic_html(_REAL_PUBLIC_AFFAIRS_CARD, "anthropic")
    assert len(items) == 1
    assert items[0]["summary"] == ""
    assert items[0]["title"] == (
        "Mariano-Florentino (Tino) Cuéllar to join Anthropic as Chief "
        "Global Affairs Officer"
    )
    # end to end: the clean headline still classifies correctly
    assert ai_labs._relevance_for(items[0]["title"], items[0]["summary"]) == ai_labs._RELEVANCE_NON_WORK


def test_real_anthropic_cards_parse_to_the_expected_dates():
    items = ai_labs._parse_anthropic_html(
        _REAL_HERO_CARD + _REAL_ENTITY_CARD + _REAL_PUBLIC_AFFAIRS_CARD, "anthropic",
    )
    assert len(items) == 3
    assert items[0]["published"] == ai_labs._parse_date("Aug 27, 2026")
    assert items[1]["published"] == ai_labs._parse_date("Aug 7, 2026")
    assert items[2]["published"] == ai_labs._parse_date("Aug 4, 2026")


# ---------------------------------------------------------------------------
# gap 5 -- health/clinical/biomedical and weather/climate/Earth-system
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lab,title,summary", [
    # all four copied verbatim from zugamind/data/scanner_cache/ai_labs.json.
    # Verified against the live 56-item cache: adding these fields flips
    # exactly these four (google_res) plus the Aurora item below, all
    # DEFAULT (0.75) -> NON-WORK (0.4), zero other items change class.
    ("google_res", "GlucoFM: Foundation model for continuous glucose monitoring",
     "Health & Bioscience"),
    ("google_res", "An AI tool for prioritizing candidate biomarkers from wearable sensor data",
     "Generative AI"),
    ("google_res", "Seeing beyond BMI: Estimating cardiometabolic risk with smartphone imagery",
     "General Science"),
    ("google_res", "Advancing AMIE towards expert-level audio-visual clinical consultations",
     "Health & Bioscience"),
])
def test_health_and_biomedical_research_is_not_builder_news(lab, title, summary):
    assert ai_labs._relevance_for(title, summary, lab) == ai_labs._RELEVANCE_NON_WORK


def test_weather_and_earth_system_research_is_not_builder_news():
    # copied verbatim from the live cache (lab "msft_research").
    title = "Aurora 1.5: Extending open foundation models for weather and Earth-system applications"
    summary = (
        "Aurora 1.5 adds 22 more variables, hourly temporal resolution, and "
        "probabilistic ensemble forecasting to the Aurora foundation model, "
        "making it more useful for real-world weather, climate, and energy "
        "applications.\nThe post Aurora 1.5: Extending open foundation models "
        "for weather and Earth-system…"
    )
    assert ai_labs._relevance_for(title, summary, "msft_research") == ai_labs._RELEVANCE_NON_WORK


def test_a_real_launch_with_clinical_in_its_own_headline_stays_high():
    """The collateral risk named in the comment above _SCI_DOMAIN_RE: this
    msft_research post is a real launch ("Introducing" satisfies HIGH) whose
    OWN headline says "Clinically Useful Radiology" -- copied verbatim from
    the live cache, including the narrow no-break space (\\u202f) the real
    feed renders between "Introducing" and "CARE-X". A bare "clinical" or
    "radiology" in _SCI_DOMAIN_RE would have demoted this launch; the fix
    pairs "clinical" only with trial/consultations/diagnos-, none of which
    "Clinically Useful" is."""
    title = (
        "Introducing CARE-X: Towards Clinically Useful Radiology VLMs "
        "with Auxiliary Supervision, Reward-Aligned Learning, and "
        "Tool-Augmented Measurement"
    )
    summary = (
        " Radiology AI is evolving beyond report generation. CARE-X explores "
        "a unified approach that combines flexible reasoning, calibrated "
        "predictions, and measurement-based tools for chest X-ray "
        "interpretation.\nThe post Introducing CARE-X: Towards Clinically "
        "Useful Radiology VLMs with Auxiliary…"
    )
    assert ai_labs._relevance_for(title, summary, "msft_research") == ai_labs._RELEVANCE_HIGH


def test_bare_clinical_without_the_paired_phrase_keeps_its_technical_reading():
    assert ai_labs._relevance_for(
        "Investigating three real-world incidents in our cybersecurity evaluations",
        "A clinical review of how our safeguards held up.",
    ) != ai_labs._RELEVANCE_NON_WORK


# ---------------------------------------------------------------------------
# gap 6 -- an undated item sorts as recent, not ancient
# ---------------------------------------------------------------------------

def test_undated_item_sorts_ahead_of_a_stale_dated_one(cache_dir):
    """`it.get("published") or 0.0` sorted an undated item BEHIND a
    700-hour-old dated one -- the opposite of the file's own doctrine that
    unknown age fails OPEN at the fresh urgency (_urgency_for treats
    published=None as fresh, 0.25). A parser regression that stops exposing
    dates must not also make the scanner treat its own posts as ancient."""
    now = time.time()
    _seed([_item(slug="baseline")])
    ai_labs.scan_ai_labs()

    stale = _item(lab="openai", slug="stale", title="Stale dated post",
                  published=now - 700 * HOUR)
    undated = _item(lab="deepmind", slug="undated", title="Undated post")  # published=None
    _seed([_item(slug="baseline"), stale, undated])
    titles = [t["title"] for t in ai_labs.scan_ai_labs()]
    assert titles.index("Undated post") < titles.index("Stale dated post")


# ---------------------------------------------------------------------------
# gap 7 -- a dark feed is louder than a quiet one, eventually
# ---------------------------------------------------------------------------

def test_repeated_fetch_failure_escalates_to_warning(cache_dir, monkeypatch, caplog):
    """Every failure used to log at debug only, forever -- a feed dark for a
    week read exactly like one that failed once five minutes ago at
    production log level. Escalated to warning at 3 in a row, mirroring
    safe_http.fetch_json's own threshold."""
    monkeypatch.setattr(ai_labs, "_CACHE_TTL", -1)  # every call re-fetches

    def always_fails(url, validators=None):
        return ("failed", None, {})
    monkeypatch.setattr(ai_labs, "_fetch", always_fails)

    with caplog.at_level(logging.WARNING, logger="zugamind.scanners.ai_labs"):
        ai_labs.scan_ai_labs()
        ai_labs.scan_ai_labs()
        assert not any("fetches in a row" in r.message for r in caplog.records)
        caplog.clear()
        ai_labs.scan_ai_labs()
        assert any("has failed 3 fetches in a row" in r.message for r in caplog.records)


def test_a_malformed_feed_logs_at_debug_instead_of_nothing(caplog):
    with caplog.at_level(logging.DEBUG, logger="zugamind.scanners.ai_labs"):
        assert ai_labs._parse_feed("<rss><this is not well-formed xml <<<", "openai") == []
        assert any("did not parse as XML" in r.message for r in caplog.records)


def test_a_malformed_hf_json_logs_at_debug_instead_of_nothing(caplog):
    with caplog.at_level(logging.DEBUG, logger="zugamind.scanners.ai_labs"):
        assert ai_labs._parse_hf_json("{not valid json") == []
        assert any("did not parse as JSON" in r.message for r in caplog.records)
