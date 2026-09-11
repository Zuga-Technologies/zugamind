"""A dark source reaches the mind, and then gets quieter on a doubling scale.

The failure this exists for: on the BugaPC twin, github_issues on a private
repo failed 1,816 consecutive times and repo_events 636 times, each emitting
one WARNING per cycle and nothing else, for weeks, while the agent reasoned as
though it could see that repo.

The failure this must not BECOME: a permanently true trigger that buys a paid
session every cycle forever. That already happened once in this codebase in a
different shape (the priority-goals clock). Most of these tests are about the
damping, not the surfacing.

Run: python -m pytest tests/scanners/test_dead_sources.py -v
"""
from __future__ import annotations

import pytest

from scanners import safe_http
from scanners.health.dead_sources import MAX_TRIGGERS, _octave, scan_dead_sources

T0 = 1_757_000_000.0


@pytest.fixture(autouse=True)
def _clean_registry():
    safe_http._DEAD_SOURCES.clear()
    yield
    safe_http._DEAD_SOURCES.clear()


def _fail(label, times, detail="HTTP 404"):
    """Drive the real recorder, so these tests break if its shape changes."""
    state = {}
    for _ in range(times):
        safe_http._record_failure(state, label, detail)
    return state


# --- the registry ------------------------------------------------------------

def test_a_source_under_the_threshold_is_not_dead_yet():
    _fail("feed:a", safe_http._FAILS_BEFORE_LOUD - 1)
    assert safe_http.dead_sources() == {}
    assert scan_dead_sources(now=T0) == []


def test_crossing_the_threshold_registers_it():
    _fail("feed:a", safe_http._FAILS_BEFORE_LOUD)
    dead = safe_http.dead_sources()
    assert list(dead) == ["feed:a"]
    assert dead["feed:a"]["fails"] == safe_http._FAILS_BEFORE_LOUD


def test_dead_sources_returns_a_copy_not_the_live_dict():
    _fail("feed:a", 5)
    snap = safe_http.dead_sources()
    snap["feed:a"]["fails"] = 999
    snap["feed:b"] = {"fails": 1}
    assert safe_http.dead_sources()["feed:a"]["fails"] == 5
    assert "feed:b" not in safe_http.dead_sources()


def test_first_seen_survives_later_failures():
    _fail("feed:a", 4)
    first = safe_http.dead_sources()["feed:a"]["first_seen"]
    _fail("feed:a", 1)
    assert safe_http.dead_sources()["feed:a"]["first_seen"] == first


# --- the trigger -------------------------------------------------------------

def test_a_dark_source_produces_one_system_health_trigger():
    _fail("github_issues:Zuga-Technologies/Ludus", 1816)
    (t,) = scan_dead_sources(now=T0)
    assert t["type"] == "system_health"
    assert t["source_label"] == "github_issues:Zuga-Technologies/Ludus"
    assert t["fails"] == 1816


def test_the_trigger_never_enters_the_alarm_lane():
    """urgency >= 0.9 bypasses the wake floor entirely. A dark feed is
    degraded, not an emergency."""
    _fail("feed:a", 9999)
    (t,) = scan_dead_sources(now=T0)
    assert t["urgency"] < 0.9


def test_system_health_is_the_degraded_branch_not_the_critical_one():
    """InfrastructureModule scores purely on trigger TYPE and COUNT; it
    ignores urgency, relevance and novelty. `system_health` must stay in the
    degraded list so one dark feed bids 0.50, under a typical learned floor."""
    _fail("feed:a", 100)
    (t,) = scan_dead_sources(now=T0)
    assert t["type"] == "system_health"
    critical = ("local_service_down", "local_systemic_failure", "production_down")
    assert t["type"] not in critical


# --- the damping, which is the point -----------------------------------------

def test_octave_buckets_by_doubling():
    assert [_octave(n, 3) for n in (3, 4, 5)] == [3, 3, 3]
    assert [_octave(n, 3) for n in (6, 11)] == [6, 6]
    assert [_octave(n, 3) for n in (12, 23)] == [12, 12]
    assert _octave(24, 3) == 24
    assert _octave(1816, 3) == 1536


def test_the_id_is_stable_while_the_streak_stays_in_one_octave():
    """The whole anti-runaway property: the same id every cycle means
    habituation damps it after the first."""
    ids = set()
    state = {}
    for _ in range(12):
        safe_http._record_failure(state, "feed:a", "HTTP 404")
    for _ in range(6):  # six more cycles inside the 12..23 octave
        safe_http._record_failure(state, "feed:a", "HTTP 404")
        (t,) = scan_dead_sources(now=T0)
        ids.add(t["id"])
    assert len(ids) == 1, ids


def test_the_id_changes_only_when_the_streak_doubles():
    state, ids = {}, []
    for _ in range(48):
        safe_http._record_failure(state, "feed:a", "HTTP 404")
        got = scan_dead_sources(now=T0)
        if got:
            ids.append(got[0]["id"])
    distinct = sorted(set(ids))
    # 48 consecutive cycles mint five ids, not forty-eight
    assert len(distinct) == 5, distinct
    assert all(i.startswith("dead_source:feed:a:") for i in distinct)


def test_the_exact_count_never_reaches_the_habituation_key():
    """_trigger_key falls back to a hash of `detail` when no id field is
    present. A `detail` that changes every cycle would defeat habituation
    outright if anyone ever dropped the id, so the octave goes in both."""
    state = {}
    details = set()
    for _ in range(12):
        safe_http._record_failure(state, "feed:a", "HTTP 404")
    for _ in range(6):
        safe_http._record_failure(state, "feed:a", "HTTP 404")
        (t,) = scan_dead_sources(now=T0)
        details.add(t["detail"])
        assert str(t["fails"]) not in t["detail"]
    assert len(details) == 1, details


def test_the_habituation_key_uses_the_id_not_the_detail_hash():
    from scanners import _trigger_key
    _fail("feed:a", 100)
    (t,) = scan_dead_sources(now=T0)
    assert _trigger_key(t) == f"system_health:{t['id']}"


# --- recovery ----------------------------------------------------------------

def test_a_success_clears_the_source():
    _fail("feed:a", 50)
    assert scan_dead_sources(now=T0)
    safe_http._DEAD_SOURCES.pop("feed:a", None)  # what the 200 path does
    assert scan_dead_sources(now=T0) == []


def test_recovery_paths_are_wired_in_the_fetcher():
    """Both places that reset the streak must also clear the registry, or a
    recovered source stays 'dark' until the process restarts."""
    import inspect
    src = inspect.getsource(safe_http)
    body = src[src.index("def fetch_json") if "def fetch_json" in src else 0:]
    assert src.count("_DEAD_SOURCES.pop(label, None)") >= 2, (
        "expected the 200 path and the 304 path to both clear the registry")


# --- the cap -----------------------------------------------------------------

def test_the_worst_sources_win_the_cap():
    for i, n in enumerate((5, 500, 50, 5000, 9)):
        _fail(f"feed:{i}", n)
    got = scan_dead_sources(now=T0)
    assert len(got) == MAX_TRIGGERS == 3
    assert [t["source_label"] for t in got] == ["feed:3", "feed:1", "feed:2"]


def test_dead_for_hours_is_reported_but_not_in_the_key():
    _fail("feed:a", 10)
    # _record_failure stamps real wall-clock; pin it so the injected `now`
    # measures against a known start.
    safe_http._DEAD_SOURCES["feed:a"]["first_seen"] = T0
    t_early = scan_dead_sources(now=T0)[0]
    t_later = scan_dead_sources(now=T0 + 7200)[0]
    assert t_early["dead_for_hours"] == 0.0
    assert t_later["dead_for_hours"] == 2.0
    assert t_later["dead_for_hours"] > t_early["dead_for_hours"]
    assert t_later["id"] == t_early["id"]
    assert t_later["detail"] == t_early["detail"]
