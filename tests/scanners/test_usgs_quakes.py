"""usgs_quakes: awareness by default, a wake only for the rare big one.

The scanner's job is to be ambient: most M4.5+ quakes are interesting, not
wake-worthy, and a wake is a paid Claude Code session. So the tests pin the
ARITHMETIC (what the workspace actually bids), not just the fields -- a
relevance number means nothing until WorldSignals turns it into a salience.
"""
from __future__ import annotations

import json
import time

import pytest

import scanners.world.usgs_quakes as uq
from cognition.workspace.workspace_modules import (
    WorldSignalsModule,
    create_all_modules,
    route_triggers_to_modules,
)

NOW = 1_800_000_000.0
WAKE_BAR_MAX = 0.6407  # highest wake floor this deployment has run (hackernews.py note)
WAKE_BAR_MIN = 0.50    # lowest


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("ZUGAMIND_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ZUGAMIND_QUAKE_PROMOTE_MAG", raising=False)
    monkeypatch.setattr(uq.time, "time", lambda: NOW)
    return tmp_path / "scanner_cache"


def _feature(eid, mag=5.0, place="10 km S of Somewhere", age_h=1.0, alert=None,
             etype="earthquake"):
    return {
        "id": eid,
        "properties": {
            "mag": mag, "place": place, "type": etype, "alert": alert,
            "time": int((NOW - age_h * 3600) * 1000), "tsunami": 0,
            "url": f"https://earthquake.usgs.invalid/{eid}",
        },
        "geometry": {"coordinates": [139.7, 35.6, 10.0]},
    }


def _serve(monkeypatch, features, status="ok"):
    """Replace the network call; returns a counter of fetches made."""
    calls = {"n": 0, "features": features, "status": status}

    def fake_fetch(state):
        calls["n"] += 1
        if calls["status"] != "ok":
            return calls["status"], None
        return "ok", {"type": "FeatureCollection", "features": calls["features"]}

    monkeypatch.setattr(uq, "_fetch", fake_fetch)
    return calls


def _sweep(monkeypatch, now):
    # Move the clock past the fetch TTL so each sweep really re-reads the feed.
    monkeypatch.setattr(uq.time, "time", lambda: now)
    return uq.scan_usgs_quakes()


def _bid(trigger):
    mod = WorldSignalsModule()
    mod.set_triggers([trigger])
    return mod.generate_bid({}).salience


def test_cold_start_baselines_silently(cache, monkeypatch):
    _serve(monkeypatch, [_feature("a"), _feature("b", mag=7.4)])
    assert uq.scan_usgs_quakes() == []  # even the M7.4: it is already old news
    assert len(json.loads((cache / "usgs_quakes_seen.json").read_text())) >= 2


def test_ambient_quake_emits_once_and_cannot_wake(cache, monkeypatch):
    calls = _serve(monkeypatch, [_feature("old")])
    uq.scan_usgs_quakes()  # baseline
    calls["features"] = [_feature("old"), _feature("new", mag=5.2)]
    out = _sweep(monkeypatch, NOW + 600)
    assert [t["id"] for t in out] == ["new"]
    t = out[0]
    assert t["type"] == "usgs_quake"
    assert t["relevance"] == pytest.approx(0.25)
    assert t["promoted"] is False
    assert _bid(t) < WAKE_BAR_MIN  # ambient must sit under every floor ever run
    # ... and it is remembered: the same sweep again emits nothing
    assert _sweep(monkeypatch, NOW + 1200) == []


def test_big_quake_is_promoted_and_clears_every_floor(cache, monkeypatch):
    calls = _serve(monkeypatch, [_feature("old")])
    uq.scan_usgs_quakes()
    calls["features"] = [_feature("old"), _feature("big", mag=7.3)]
    (t,) = _sweep(monkeypatch, NOW + 600)
    assert t["promoted"] is True and t["relevance"] == pytest.approx(0.9)
    assert _bid(t) > WAKE_BAR_MAX


def test_pager_orange_promotes_below_the_magnitude_line(cache, monkeypatch):
    calls = _serve(monkeypatch, [_feature("old")])
    uq.scan_usgs_quakes()
    calls["features"] = [_feature("old"), _feature("dmg", mag=6.1, alert="orange")]
    (t,) = _sweep(monkeypatch, NOW + 600)
    assert t["promoted"] is True


@pytest.mark.parametrize("mag,alert,age_h", [
    (5.0, None, 0.1), (7.0, None, 0.1), (9.5, "red", 0.1), (9.5, "red", 400.0),
])
def test_urgency_never_reaches_the_alarm_lane(cache, monkeypatch, mag, alert, age_h):
    # urgency >= 0.9 bypasses bidding and goes straight to the alarm lane.
    # A quake is news, not an outage: no input may ever get there.
    calls = _serve(monkeypatch, [_feature("old")])
    uq.scan_usgs_quakes()
    calls["features"] = [_feature("old"), _feature("x", mag=mag, alert=alert, age_h=age_h)]
    (t,) = _sweep(monkeypatch, NOW + 600)
    assert 0.0 <= t["urgency"] < 0.9


def test_revision_across_the_line_promotes_once_and_downgrade_is_silent(cache, monkeypatch):
    calls = _serve(monkeypatch, [_feature("old")])
    uq.scan_usgs_quakes()
    calls["features"] = [_feature("old"), _feature("q", mag=6.8)]
    (first,) = _sweep(monkeypatch, NOW + 600)
    assert first["promoted"] is False
    # USGS revises the magnitude up within hours: that IS new information.
    calls["features"] = [_feature("old"), _feature("q", mag=7.1)]
    (second,) = _sweep(monkeypatch, NOW + 1200)
    assert second["promoted"] is True and second["id"] == "q"
    # ... but it fires once, and a later revision back down is not a re-announcement.
    assert _sweep(monkeypatch, NOW + 1800) == []
    calls["features"] = [_feature("old"), _feature("q", mag=6.9)]
    assert _sweep(monkeypatch, NOW + 2400) == []


def test_non_earthquakes_and_null_magnitudes_are_dropped(cache, monkeypatch):
    calls = _serve(monkeypatch, [_feature("old")])
    uq.scan_usgs_quakes()
    calls["features"] = [
        _feature("old"),
        _feature("blast", mag=5.0, etype="quarry blast"),
        _feature("nomag", mag=None),
    ]
    assert _sweep(monkeypatch, NOW + 600) == []


def test_emit_cap_keeps_promoted_over_a_flood_of_ambient(cache, monkeypatch):
    calls = _serve(monkeypatch, [_feature("old")])
    uq.scan_usgs_quakes()
    flood = [_feature(f"a{i}", mag=4.6, age_h=0.5 + i * 0.01) for i in range(12)]
    calls["features"] = [_feature("old"), *flood, _feature("big", mag=7.0, age_h=9.0)]
    out = _sweep(monkeypatch, NOW + 600)
    assert len(out) <= 5
    assert out[0]["id"] == "big"  # the rare big one is never crowded out by newer small ones


@pytest.mark.parametrize("status", ["failed", "rate_limited"])
def test_failed_fetch_keeps_cached_events_and_stays_quiet(cache, monkeypatch, status):
    calls = _serve(monkeypatch, [_feature("old")])
    uq.scan_usgs_quakes()
    calls["status"] = status
    assert _sweep(monkeypatch, NOW + 600) == []
    # a transient miss must not wipe a known-good set (that would re-baseline / go dark)
    calls["status"] = "ok"
    calls["features"] = [_feature("old"), _feature("new", mag=5.5)]
    assert [t["id"] for t in _sweep(monkeypatch, NOW + 1200)] == ["new"]


def test_wrong_shaped_body_is_a_failure_not_an_empty_feed(cache, monkeypatch):
    calls = _serve(monkeypatch, [_feature("old")])
    uq.scan_usgs_quakes()
    monkeypatch.setattr(uq, "_fetch", lambda state: ("ok", {"error": "login wall"}))
    assert _sweep(monkeypatch, NOW + 600) == []
    state = json.loads((cache / "usgs_quakes.json").read_text())
    assert state["events"], "a 200 that is not a FeatureCollection must not erase the cache"


def test_fetch_is_ttl_gated(cache, monkeypatch):
    calls = _serve(monkeypatch, [_feature("old")])
    uq.scan_usgs_quakes()
    assert calls["n"] == 1
    _sweep(monkeypatch, NOW + 30)
    assert calls["n"] == 1  # inside the TTL: no second request to USGS
    _sweep(monkeypatch, NOW + uq._CACHE_TTL_SEC + 1)
    assert calls["n"] == 2


def test_garbage_promote_env_falls_back_to_default(cache, monkeypatch):
    monkeypatch.setenv("ZUGAMIND_QUAKE_PROMOTE_MAG", "seven")
    assert uq._promote_mag() == 7.0
    monkeypatch.setenv("ZUGAMIND_QUAKE_PROMOTE_MAG", "6.5")
    assert uq._promote_mag() == 6.5


def test_usgs_quake_is_routed_to_world_signals_not_silently_dropped(cache, monkeypatch):
    # route_triggers_to_modules drops any type no module claims. Without the
    # entry in WorldSignals.TRIGGER_TYPES this scanner would run, fetch, dedupe
    # and emit -- into nothing, with no error anywhere.
    calls = _serve(monkeypatch, [_feature("old")])
    uq.scan_usgs_quakes()
    calls["features"] = [_feature("old"), _feature("new", mag=5.2)]
    triggers = _sweep(monkeypatch, NOW + 600)
    modules = create_all_modules()
    route_triggers_to_modules(triggers, modules)
    world = next(m for m in modules if m.name == "world_signals")
    assert [t["id"] for t in world._triggers] == ["new"]


def test_scheduler_knows_this_source():
    from scanners.scheduler import _STATIC_SPECS
    spec = _STATIC_SPECS["scan_usgs_quakes"]
    assert spec.base_cadence_secs >= 300  # be a polite poller of a public service
