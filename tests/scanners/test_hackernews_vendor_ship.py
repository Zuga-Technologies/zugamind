"""The HackerNews scanner had one relevance number, so it could only ever be
fully deaf or fully open -- and it has now been observed in both states.

Two regressions, three days apart, which turned out to be one defect seen from
either side. The flat relevance is the defect; which way it fails is decided by
where the wake floor happens to be sitting that night.

  2026-09-01 -- DEAF. The scanner's ceiling (0.58) sat under the floor
  (0.56-0.59), so nothing it ever emitted could wake. See the vendor-ship
  section below.

  2026-09-03 -- OPEN. Floor drift dropped the bar to 0.5042, under the
  scanner's FLOOR (0.510), and a 6-point blog post bought a session. A sweep of
  the live cache put 6 of 6 keyword-passing stories over the bar -- including a
  1-point YC job ad. See the ambient-tier section at the bottom.

Regression for the 2026-09-01 18:07Z filter. `HN [27pts]: Claude Fable 5.1 and
Claude Mythos 5.1` -- our primary vendor shipping two models -- bid raw 0.537
against a 0.580 floor and was dropped. Seven minutes later an unrecognized post
on Anthropic's own PR feed ("Developing Enterprise Frontier Safeguards with our
customers") bid 0.600 and bought a Claude session.

That was not bad luck, it was arithmetic. Every story got relevance 0.5, flat.
WorldSignals prices a bid at 0.25 + 0.4*relevance + 0.2*urgency and this
scanner caps urgency at 0.65, so the ceiling for ANY story was:

    0.25 + 0.4(0.5) + 0.2(0.65) = 0.58

...against a floor that ran 0.56-0.59 all week and calibrates to 0.6407. The
one escape hatch, `_BRAND_RE` at relevance 0.9, is dark: ZUGAMIND_BRAND_TERMS
has never been set in this deployment. Measured over the journal: the scanner
won the global workspace 52 times in four days and woke 0 times, while 100% of
external wakes came from lab PR feeds.

The second half of these tests is why the gate is a conjunction and not either
half alone. A vendor name by itself is ambient chatter, and ship-grammar by
itself, on a firehose like HN, is every "Show HN: my new SDK" ever posted. A
promotion that reaches those makes the scanner loud instead of deaf -- strictly
worse than the silence it replaces.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "zugamind") not in sys.path:
    sys.path.insert(0, str(_ROOT / "zugamind"))

from scanners.world import hackernews  # noqa: E402


def _raw_bid(relevance: float, urgency: float) -> float:
    """WorldSignals' price for a trigger, so the assertions are in the units
    that actually decide whether a session gets bought. This is the number the
    wake floor compares against -- the floor takes min(bid, modulated), so an
    excited state can only ever lower it, never rescue a bid from under."""
    return round(min(0.75, 0.25 + 0.4 * relevance + 0.2 * urgency), 4)


# ---------------------------------------------------------------------------
# The arithmetic that made the scanner mute
# ---------------------------------------------------------------------------

def test_flat_relevance_could_never_clear_the_floor():
    """The bug, stated as a number. At relevance 0.5 the best possible story --
    maximum urgency, front page, thousands of points -- tops out at 0.58, under
    every floor this deployment has run."""
    assert _raw_bid(0.5, 0.65) == 0.58
    assert _raw_bid(0.5, 0.65) < 0.6407     # calibrated floor
    assert _raw_bid(0.5, 0.65) < 0.58 + 1e-9  # and 0.58 is the CEILING, not a typical bid


def test_promoted_stories_clear_the_floor_even_with_no_upvotes():
    """0.9 is chosen to clear a floor, not to express enthusiasm. 0.3 is the
    urgency floor in scan_hackernews (the old flat prior), i.e. a story nobody
    upvoted -- it still has to get through."""
    assert _raw_bid(0.9, 0.3) == 0.67
    assert _raw_bid(0.9, 0.3) > 0.6407
    # ...and it still cannot reach the alarm lane, which reads trigger urgency
    # (>= 0.9) and not bid salience. A model launch is news, not an outage.
    assert _raw_bid(0.9, 0.65) <= 0.75      # WorldSignals' _SALIENCE_CAP


# ---------------------------------------------------------------------------
# The four real stories this was built for
# ---------------------------------------------------------------------------

def test_the_filtered_launch_now_promotes():
    """The story that caused this test file."""
    assert hackernews._is_vendor_ship("Claude Fable 5.1 and Claude Mythos 5.1")


def test_usage_terms_changes_promote():
    """Both of these were swallowed in the 52. A 25% cut to the usage limits of
    the tool this operation runs on is the single most actionable thing HN
    surfaced all week, and it scored the same as "I hate AI images and music"."""
    for title in (
        "Claude Code is going reduce limits by 25% from September 14",
        "Claude permanently raising weekly limits by 25%",
    ):
        assert hackernews._is_vendor_ship(title), title


def test_deprecation_and_outage_grammars_promote():
    for title in (
        "OpenAI is deprecating the Assistants API",
        "Anthropic sunsetting Claude 2 on the API",
        "OpenAI outage affecting the Codex endpoint",
        "Gemini pricing changes land next month",
    ):
        assert hackernews._is_vendor_ship(title), title


# ---------------------------------------------------------------------------
# Why it is a conjunction: neither half is safe alone
# ---------------------------------------------------------------------------

def test_a_vendor_name_alone_is_not_a_ship():
    """Ambient chatter that mentions a vendor. Promoting these trades a deaf
    scanner for a chatty one. All five are real titles from the 52."""
    for title in (
        "Warp builds self-improving agents on Claude",
        "Claude Session URL appended to commit messages and PR descriptions by default",
        "EFF to Courts: Don't Rewrite Copyright over AI Hype",
        "What my dad taught me about AI coding in the 90s",
        "Sony Music and Warner Chappell are suing Anthropic",
    ):
        assert not hackernews._is_vendor_ship(title), title


def test_ship_grammar_alone_is_not_a_ship():
    """The firehose half of the argument. ai_labs can trust bare ship-grammar
    because every item on its feed is already a lab post; HN cannot -- these
    would all promote if the vendor gate were dropped."""
    for title in (
        "OpenShot 4.0 - Open-source video editor",
        "Show HN: Typebase - A single-folder back end you write in TypeScript",
        "I trained a small transformer in 1.5hrs and it beats many LLMs",
        "Launch HN: Almanac (YC S26) - AI that knows your company",
        "EVE Online moves to Python 3",
    ):
        assert not hackernews._is_vendor_ship(title), title


def test_openshot_does_not_read_as_openai():
    """A substring away from a false promotion on every release it ever cuts."""
    assert not hackernews._VENDOR_RE.search("OpenShot 4.0 - Open-source video editor")


def test_bare_usage_words_need_a_vendor():
    """"limits", "quota" and "pricing" are ordinary English. Matched bare they
    would promote any startup's pricing post on the front page."""
    for title in (
        "Rethinking rate limits for public APIs",
        "Our new pricing, explained",
        "Postgres connection quota exhaustion in production",
    ):
        assert not hackernews._is_vendor_ship(title), title


# ---------------------------------------------------------------------------
# Blast radius, measured on the real corpus
# ---------------------------------------------------------------------------

_LIVE_CORPUS = [
    "Apple Caught Off Guard by AI Demand for Mac Mini and Mac Studio",
    "Artie (YC S23) Is Hiring Technical AES",
    "Autonomous (YC F25) Is Hiring Engineers",
    "Benchmarking Pocket-Scale Inference",
    "Breaking Claude Code Opus 5 Auto Mode",
    "Claude Code is going reduce limits by 25% from September 14",
    "Claude Fable 5.1 and Claude Mythos 5.1",
    "Claude Session URL appended to commit messages and PR descriptions by default",
    "Claude permanently raising weekly limits by 25%",
    "CollectWise (YC F24) Is Hiring",
    'Debian votes to allow "responsible use of generative AI"',
    "DoltLite: A SQLite fork with Git-style version control, built with 2k agent PRs",
    "EFF to Courts: Don't Rewrite Copyright over AI Hype",
    "EVE Online moves to Python 3",
    "Functional State Machines in Rust: Typestate and Newtype Patterns",
    "German Konrad Zuse Museum shutting down due to lack of funding",
    "Good Culture Is the Biggest Productivity Hack, Not AI",
    "Google Antigravity introduces Boost deep reasoning (/boost)",
    "I built a forgetting curve for an agent with one user",
    "I hate AI images and music",
    "I trained a small transformer in 1.5hrs and it beats many LLMs",
    "Is it safe to call print in a Python signal handler?",
    "Launch HN: Almanac (YC S26) - AI that knows your company",
    "METR and Redwood Offer Holy %^ Postmortem of the HuggingFace Hack",
    "Nvidia's AI advantage is moving beyond the GPU",
    "Open Oscar Server: open-source server compatible with AIM and ICQ clients",
    "OpenShot 4.0 - Open-source video editor",
    "Our decision on Cursor following its acquisition by SpaceX",
    "Quill (YC W20) Is Hiring a Fullstack SWE",
    "Show HN: FnScribe - Open-source, offline dictation for macOS",
    "Show HN: Linux server management over SSH - written in Rust and Tauri",
    "Show HN: Typebase - A single-folder back end you write in TypeScript",
    "Startup Anti-Patterns",
    "StemDeck, a free, open-source and local AI stem separator",
    "The Finn - an agent that lives in my router and complains about it",
    "The Internet Archive's Vintage AI Collection",
    "The Rise and Fall of Agent Civilizations",
    "Warp builds self-improving agents on Claude",
    "What my dad taught me about AI coding in the 90s",
    "Why open source rocks - a new SM750 (Silicon Motion GPU) HDMI Driver",
]


def test_blast_radius_on_the_real_corpus():
    """Every distinct title this scanner emitted 2026-08-29..2026-09-01. The
    point of pinning the exact set: this promotion spends real sessions, and a
    later loosening of the grammar should have to change this list on purpose
    rather than discover the cost in the budget ledger."""
    promoted = {t for t in _LIVE_CORPUS if hackernews._is_vendor_ship(t)}
    assert promoted == {
        "Breaking Claude Code Opus 5 Auto Mode",
        "Claude Code is going reduce limits by 25% from September 14",
        "Claude Fable 5.1 and Claude Mythos 5.1",
        "Claude permanently raising weekly limits by 25%",
    }, sorted(promoted)


def test_the_scanner_stays_mostly_quiet():
    """36 of 40 keep the ambient tier. This scanner's job is to be silent."""
    promoted = [t for t in _LIVE_CORPUS if hackernews._is_vendor_ship(t)]
    assert len(promoted) / len(_LIVE_CORPUS) <= 0.15


# ---------------------------------------------------------------------------
# The ambient tier: the same defect, seen from the other side (2026-09-03)
# ---------------------------------------------------------------------------

# Every calibrated raw floor this deployment is known to have run, lowest
# first. 0.5042 is tonight's, and it is the one that opened the scanner.
_FLOORS_SEEN = (0.5042, 0.5073, 0.5708, 0.58, 0.6407)


def test_the_old_flat_relevance_put_every_story_over_tonights_floor():
    """The 2026-09-03 bug, stated as a number. At a flat 0.5 the scanner's
    whole range was [0.510, 0.580] -- and its FLOOR was above tonight's bar, so
    there was no such thing as an HN story too boring to buy a session."""
    cheapest = _raw_bid(0.5, 0.3)    # zero upvotes, oldest possible story
    dearest = _raw_bid(0.5, 0.65)    # front page, maximum velocity
    assert (cheapest, dearest) == (0.51, 0.58)
    assert cheapest > 0.5042, "the cheapest possible story still cleared the bar"
    # ...and the whole dynamic range was 0.07 -- barely more than one
    # CALIBRATION_MARGIN (0.05). That is why no value of a single flat number
    # could have worked: there was never more than a margin's worth of room
    # inside the scanner to tell a front-page story from a job ad, so the bar
    # could only ever land above the whole band or below the whole band.
    assert dearest - cheapest < 0.05 * 2


def test_ambient_stories_sit_under_every_floor_this_deployment_has_run():
    """A broad topical keyword match is the base rate of HackerNews, not news.
    _KEEP_RE matches "Python", "model", "agent", "startup" -- so the ambient
    tier has to lose to every bar we have actually seen, including the lowest.

    WARMUP_FLOOR is in the list on purpose. This docstring used to call the
    pre-calibration floor "a known, accepted gap" that ambient bids could not
    be asserted against. That premise was false: WARMUP_FLOOR is also the
    lower CLAMP on every calibrated floor, so it is the bar on every quiet
    night -- and on 2026-09-06 a 4-point story bid 0.41 against it and woke a
    session. The ambient tier has to lose to the clamp too."""
    from act.floor_calibration import WARMUP_FLOOR
    cheapest = _raw_bid(hackernews._RELEVANCE_AMBIENT, 0.3)
    dearest = _raw_bid(hackernews._RELEVANCE_AMBIENT, 0.65)
    assert (cheapest, dearest) == (0.41, 0.48)
    for floor in _FLOORS_SEEN + (WARMUP_FLOOR,):
        assert dearest < floor, f"ambient tier reaches the {floor} floor"


def test_promoted_stories_clear_every_floor_this_deployment_has_run():
    """The other half of the pair: the lanes must not BOTH be silent. A vendor
    ship nobody upvoted still has to get through the highest bar we have run."""
    cheapest = _raw_bid(hackernews._RELEVANCE_PROMOTED, 0.3)
    assert cheapest == 0.67
    for floor in _FLOORS_SEEN:
        assert cheapest > floor, f"promoted tier cannot clear the {floor} floor"


def test_the_lanes_are_further_apart_than_the_calibration_margin():
    """This is the property that makes the scanner degrade instead of switch.

    The floor is calibrated as the 90th percentile of ambient winners + a
    CALIBRATION_MARGIN of 0.05, so it is definitionally dragged to just above
    whatever this scanner emits at volume. That only discriminates if the real
    signal sits more than a margin above the ambient chatter. A future edit
    that narrows this gap re-opens the 2026-09-03 firehose."""
    gap = (_raw_bid(hackernews._RELEVANCE_PROMOTED, 0.3)
           - _raw_bid(hackernews._RELEVANCE_AMBIENT, 0.65))
    assert gap > 0.05 * 3, f"lane gap {gap:.3f} is too thin to survive floor drift"


# ---------------------------------------------------------------------------
# Wiring: the constants above are the ones scan_hackernews actually stamps
# ---------------------------------------------------------------------------

def _one_story(tmp_path, monkeypatch, title, score=6):
    """Drive a real scan cycle with one controllable story. The first call
    baselines the front page silently (cold start), so the story under test is
    introduced as a NEW entrant on the second call -- which is the only path
    that actually emits."""
    monkeypatch.setattr(hackernews, "_CACHE_PATH", tmp_path / "hackernews.json")
    state = {"ids": [1], "titles": {1: "Seed story about Python"}}

    def fake_fetch(url):
        if url == hackernews._TOP_URL:
            return list(state["ids"])
        sid = int(url.split("/item/")[1].split(".json")[0])
        return {"title": state["titles"][sid], "url": "http://x", "score": score,
                "time": 1000.0}

    monkeypatch.setattr(hackernews, "_fetch_json", fake_fetch)
    monkeypatch.setattr(hackernews.time, "time", lambda: 1000.0)
    assert hackernews.scan_hackernews() == []          # cold-start baseline
    state["ids"] = [1, 2]
    state["titles"][2] = title
    monkeypatch.setattr(hackernews.time, "time",
                        lambda: 1000.0 + hackernews._top_ttl() + 1)
    out = [t for t in hackernews.scan_hackernews() if t["story_id"] == 2]
    assert len(out) == 1, f"story was filtered out entirely: {title!r}"
    return out[0]


def test_scan_stamps_the_ambient_tier(tmp_path, monkeypatch):
    trig = _one_story(tmp_path, monkeypatch, "EVE Online moves to Python 3")
    assert trig["relevance"] == hackernews._RELEVANCE_AMBIENT
    assert "vendor_ship" not in trig
    assert trig["detail"].startswith("HN [")


def test_scan_stamps_the_promoted_tier_for_a_vendor_ship(tmp_path, monkeypatch):
    trig = _one_story(tmp_path, monkeypatch,
                      "Claude permanently raising weekly limits by 25%")
    assert trig["relevance"] == hackernews._RELEVANCE_PROMOTED
    assert trig["vendor_ship"] is True
    assert trig["detail"].startswith("HN VENDOR SHIP [")


def test_tonights_wake_would_not_have_happened(tmp_path, monkeypatch):
    """The end-to-end regression, replayed on the real title and score.

    `HN [6pts]: No--AI Agents Did Not Build Secret Civilizations Stop
    Anthropomorphizing Malware` bid 0.5156 against a 0.5042 bar and bought a
    session. It is media criticism of media criticism: no model, no API, no
    pricing, no deprecation."""
    trig = _one_story(
        tmp_path, monkeypatch,
        "No--AI Agents Did Not Build Secret Civilizations Stop "
        "Anthropomorphizing Malware")
    assert trig["relevance"] == hackernews._RELEVANCE_AMBIENT
    assert _raw_bid(trig["relevance"], trig["urgency"]) < 0.5042
