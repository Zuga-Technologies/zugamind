"""The self-calibrating floor cannot price a rare source — measured live
2026-09-04, from the BugaPC deployment's own journal.

`tests/scanners/test_ai_labs.py` prices an unrecognized post on a curated
feed at exactly 0.600 and leans on the moving floor to catch the overflow.
Two of its docstrings say so outright:

    "at 0.600 a model launch cleared it by exactly zero — three more such
     winners would have ratcheted it to 0.65 and silenced the feed"

    "its own 0.600 bids become the p90 that lifts the floor it has to clear"

That ratchet is the entire reason the DEFAULT tier is allowed to fail open
*above* the floor rather than below it. It has never fired, and it cannot:

  - `QUANTILE` is 0.9, so the floor tracks the top **10%** of the window.
    For it to out-price a 0.600 bid, at least a tenth of every sample in
    the window must be >= 0.550.
  - DEFAULT-tier `ai_lab_research` winners arrived **9 times in 6.8 days**
    on this deployment — one per 18.0h. The measured median workspace cycle
    is 422s, so they are **0.65% of cycles**. Against a 10% boundary that
    is short by a factor of **15**.
  - The quantile is a *fraction*, not a rank, so this is scale-invariant:
    widening `ROLLING_WINDOW` scales the boundary rank with the window and
    changes nothing. There is no window size at which the ratchet engages.

Measured consequence over that window: 26 `floor_drifted` events, raw-basis
floor ranging 0.4943-0.5900, **maximum 0.590** — never once at the 0.600 it
would have to reach. All 9 DEFAULT winners converted to harness wakes, a
100% conversion rate, making the fail-open tier the largest single wake
consumer in the system (8 completed invocations out of 28 in that window;
the 9th is the session that wrote this file).

Why the live floor sits at 0.59 at all: the top decile is filled by the
*aggregate* of every wake-eligible source (dirty_studio 0.61, hivemind goal
events 0.65, news_rss 0.55, session_handoff 0.54, ai_labs). The floor
prices the aggregate, which is exactly why it can never price a member of
it. Any single scanner that fails open above the floor has bought itself a
permanent wake.

These tests pin the diagnosis so it is not re-derived at the next wake.
The last one is the control: hold the floor code fixed and raise only the
source's *share* of cycles past the quantile boundary, and the ratchet the
ai_labs suite already assumes does fire — proving the mechanism works and
the share is what is missing.
"""
from __future__ import annotations

import math

import pytest

import act.floor_calibration as floor_calibration
import continuity.journal as journal

# Measured median workspace cycle gap, 2039 records over 6.8 days.
CYCLE_SECONDS = 422
# DEFAULT-tier ai_lab_research winners: 9 in 6.8 days => one per 18.0h.
ARRIVAL_HOURS = 18.0
CYCLES_BETWEEN_ARRIVALS = round(ARRIVAL_HOURS * 3600 / CYCLE_SECONDS)  # 154

# What an unrecognized post on a curated feed bids fresh:
# 0.25 + 0.4 * _RELEVANCE_DEFAULT(0.75) + 0.2 * urgency(0.25).
DEFAULT_TIER_BID = 0.60
# Idle winners as they appear in the live raw series — priority_goals and
# metacognition chatter, which is what the window is nearly all made of.
IDLE_NOISE = [0.23, 0.2236, 0.2147, 0.26, 0.29, 0.2177, 0.32, 0.2251]


def _patch(tmp_path, monkeypatch):
    monkeypatch.setattr(floor_calibration, "STATE_FILE", tmp_path / "floor_calibration.json")
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")


def _winner(raw_salience):
    """A workspace winner carrying its pre-modulation salience — what the raw
    series records and what the live gate actually compares."""
    return {
        "source_module": "world_signals",
        "salience": raw_salience,
        "context": {"raw_salience": raw_salience},
    }


def _window(size, arrival_every):
    """A rolling window in the live shape: idle chatter, with a DEFAULT-tier
    winner every `arrival_every` cycles."""
    return [
        DEFAULT_TIER_BID if (i and i % arrival_every == 0) else IDLE_NOISE[i % len(IDLE_NOISE)]
        for i in range(size)
    ]


def test_default_tier_never_ratchets_the_floor_in_the_live_regime(tmp_path, monkeypatch):
    """End-to-end through the real recording path, one week of the measured
    regime. The floor never reaches the 0.600 it must exceed, so every
    DEFAULT-tier post in that week buys a harness wake."""
    _patch(tmp_path, monkeypatch)
    hc = {"name": "claude-code", "wake_min_salience": "calibrate"}

    highest = 0.0
    for i in range(round(7 * 24 * 3600 / CYCLE_SECONDS)):  # 1434 cycles
        sample = (DEFAULT_TIER_BID if (i and i % CYCLES_BETWEEN_ARRIVALS == 0)
                  else IDLE_NOISE[i % len(IDLE_NOISE)])
        floor_calibration.maybe_record_ambient_sample(hc, _winner(sample))
        floor, _ = floor_calibration.resolve_gate("claude-code")
        highest = max(highest, floor)

    assert highest < DEFAULT_TIER_BID
    # Matches the live maximum: 26 floor_drifted events, raw floor max 0.590.
    assert highest <= 0.59


@pytest.mark.parametrize("window", [50, 100, 200, 500, 1000, 2000])
def test_no_window_size_lets_the_ratchet_engage(window):
    """Scale-invariance: `QUANTILE` is a fraction, so the boundary rank grows
    with the window in lockstep with the arrivals it would have to hold.
    Widening `ROLLING_WINDOW` is not the fix — at 2000 samples (9.8 days of
    memory) the floor is no closer to pricing this source than at 50."""
    floor = floor_calibration._quantile_floor(_window(window, CYCLES_BETWEEN_ARRIVALS))
    assert floor < DEFAULT_TIER_BID, f"window={window} unexpectedly ratcheted to {floor}"


def test_the_quantile_is_a_fraction_so_a_rare_source_cannot_price_itself():
    """The bug as arithmetic rather than prose.

    A quantile floor responds only to the top (1 - QUANTILE) share of the
    window. A source that occupies a smaller share of cycles than that is
    invisible to it, no matter how expensive each of its wakes is.
    """
    responds_to_share = 1.0 - floor_calibration.QUANTILE      # 0.10
    default_tier_share = 1.0 / CYCLES_BETWEEN_ARRIVALS        # 0.0065

    assert default_tier_share < responds_to_share
    assert responds_to_share / default_tier_share == pytest.approx(15.4, abs=0.5)


def test_the_ratchet_engages_once_the_source_exceeds_the_quantile_share():
    """The control — the floor code is not broken, the share is.

    Hold everything fixed and make DEFAULT-tier winners arrive often enough
    to occupy more than the top decile (every 8th cycle = 12.5% > 10%). The
    self-silencing the ai_labs suite assumes then happens exactly as its
    docstring describes.
    """
    every = 8
    assert 1.0 / every > 1.0 - floor_calibration.QUANTILE

    floor = floor_calibration._quantile_floor(_window(500, every))
    assert floor > DEFAULT_TIER_BID
    # ...and lands on the 0.65 the ai_labs docstring predicted.
    assert floor == pytest.approx(0.65)


def test_the_boundary_rank_scales_with_the_window():
    """Why the previous test's `every` is a share and not a count: the number
    of high samples the p90 needs is a fixed fraction of the window, so it
    grows exactly as fast as a longer window's extra arrivals.

    Nearest-rank rounds one sample conservative, which is visible only at
    small windows (6/50 = 12% rather than 10%) and washes out as it grows.
    """
    for window in (50, 500, 5000):
        rank_from_top = window - (math.ceil(floor_calibration.QUANTILE * window) - 1)
        share = rank_from_top / window
        assert share == pytest.approx(1 - floor_calibration.QUANTILE, abs=0.025)
