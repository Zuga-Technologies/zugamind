"""USGS earthquake scanner -- real-world awareness, ambient by default.

Free public feed, no key, no cost: USGS's M4.5+ "past day" GeoJSON summary
(https://earthquake.usgs.gov/earthquakes/feed/v1.0/). It updates about once a
minute; we poll it far more politely than that, through scanners.safe_http
(conditional GET, 429 backoff, credential-safe redirects).

WHY THIS SCANNER IS MOSTLY QUIET, ON PURPOSE
--------------------------------------------
A trigger wakes a paid Claude Code session when WorldSignals prices it over the
wake floor:

    salience = min(0.75, 0.25 + 0.4*relevance + 0.2*urgency)      floor ~0.50-0.64

Roughly 10-20 quakes of M4.5+ happen every day. Almost none of them change
anything we build, so they are AWARENESS, not wake-worthy -- the same lesson
hackernews.py and news_rss.py each learned the hard way (a flat relevance is
not a judgement, it is the absence of one). Two tiers, far enough apart that
floor drift cannot flip one into the other:

    AMBIENT   relevance 0.25, urgency <= 0.3   ->  bid <= 0.41   never wakes
    PROMOTED  relevance 0.90, urgency <= 0.6   ->  bid ~0.73     wakes

PROMOTED is a rule in code, not a mood: magnitude >= ZUGAMIND_QUAKE_PROMOTE_MAG
(default 7.0, roughly a dozen a year worldwide) or a USGS PAGER alert of orange
or red (estimated casualties/damage -- this can fire below the magnitude line).

URGENCY IS CAPPED BELOW 0.9, ALWAYS. A trigger with urgency >= 0.9 skips the
bid and goes straight to the alarm lane (WorldSignals docstring). A quake is
news, not an outage; no input here may reach that lane.

Dedupe: identity is USGS's own event id, emitted under `id`. USGS REVISES
magnitudes for hours after an event, so the seen-set has two keys per event --
`<id>` (announced as ambient) and `<id>!` (announced as promoted). A quake
first reported M6.8 and revised to M7.1 therefore fires once more, as promoted;
a revision back down fires nothing. First run baselines the whole feed silently
(scanners.seen_items cold-start rule): the past day's quakes are not news.

Stdlib only. Fail-silent: any fetch problem returns [] and keeps the cached
events, so a transient miss never wipes the known-good set.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from foundation.fs import atomic_write_text
from scanners import safe_http
from scanners.seen_items import read_seen, write_seen

logger = logging.getLogger("zugamind.scanners.usgs_quakes")

_FEED_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"
_CACHE_TTL_SEC = 300
_MAX_TRIGGERS = 5
# ~10-20 events/day, each stamped twice at worst; this holds weeks of history.
_SEEN_MAX = 600
_MAX_CACHED_EVENTS = 200

_RELEVANCE_AMBIENT = 0.25
_RELEVANCE_PROMOTED = 0.9
_DEFAULT_PROMOTE_MAG = 7.0
_PROMOTE_ALERTS = ("orange", "red")  # USGS PAGER: estimated damage / casualties

_FRESH_HOURS = 12
# (fresh, stale). Promoted tops out at 0.6 and ambient at 0.3: the alarm lane
# starts at 0.9 and nothing in this file may get near it.
_URGENCY_AMBIENT = (0.3, 0.1)
_URGENCY_PROMOTED = (0.6, 0.3)


def _data_dir() -> Path:
    # Read at call time (not import time) so tests and a re-pointed
    # ZUGAMIND_DATA_DIR take effect without re-importing the module.
    return Path(os.environ.get("ZUGAMIND_DATA_DIR")
                or Path(__file__).resolve().parent.parent.parent / "data")


def _state_path() -> Path:
    return _data_dir() / "scanner_cache" / "usgs_quakes.json"


def _seen_path() -> Path:
    return _data_dir() / "scanner_cache" / "usgs_quakes_seen.json"


def _promote_mag() -> float:
    """Tolerant, call-time read: a typo'd env var must degrade to the default,
    never raise (hackernews' bare module-level float() once stopped the whole
    runner from booting over a typo)."""
    raw = (os.environ.get("ZUGAMIND_QUAKE_PROMOTE_MAG") or "").strip()
    if not raw:
        return _DEFAULT_PROMOTE_MAG
    try:
        return float(raw)
    except ValueError:
        logger.warning("usgs_quakes: ZUGAMIND_QUAKE_PROMOTE_MAG=%r is not a "
                       "number -- using %s", raw, _DEFAULT_PROMOTE_MAG)
        return _DEFAULT_PROMOTE_MAG


def _load_state() -> dict:
    try:
        path = _state_path()
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                if not isinstance(raw.get("http"), dict):
                    raw["http"] = {}
                if not isinstance(raw.get("events"), list):
                    raw["events"] = []
                return raw
    except Exception as e:  # noqa: BLE001
        logger.debug("usgs_quakes state load failed: %s", e)
    return {"http": {}, "events": [], "last_fetched": 0.0}


def _save_state(state: dict) -> None:
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(state))
    except Exception as e:  # noqa: BLE001
        logger.warning("usgs_quakes state save failed: %s", e)


def _fetch(state: dict) -> tuple:
    """The one network seam (tests replace it). Returns (status, data)."""
    return safe_http.fetch_json(_FEED_URL, state=state, name="usgs_quakes")


def _parse(features: list) -> list[dict]:
    """GeoJSON features -> small flat events. Drops anything that is not a
    real earthquake with a magnitude (USGS also lists quarry blasts and
    explosions under the same feed)."""
    out: list[dict] = []
    for f in features:
        try:
            props = f.get("properties") or {}
            eid = str(f.get("id") or "").strip()
            mag = props.get("mag")
            if not eid or mag is None or props.get("type", "earthquake") != "earthquake":
                continue
            coords = (f.get("geometry") or {}).get("coordinates") or [None, None, None]
            out.append({
                "id": eid,
                "mag": float(mag),
                "place": str(props.get("place") or "unknown location")[:120],
                "time": safe_http.num(props.get("time")) / 1000.0,
                "url": str(props.get("url") or ""),
                "alert": props.get("alert") or None,
                "tsunami": int(safe_http.num(props.get("tsunami"))),
                "lon": coords[0], "lat": coords[1],
                "depth_km": coords[2] if len(coords) > 2 else None,
            })
        except Exception:  # noqa: BLE001 -- one malformed feature must not sink the sweep
            continue
    out.sort(key=lambda e: e["time"], reverse=True)
    return out[:_MAX_CACHED_EVENTS]


def _events(state: dict, now: float) -> list[dict]:
    """Cached events, refreshed from USGS at most once per _CACHE_TTL_SEC.

    The attempt is stamped whatever the outcome, so a persistently failing
    feed costs one request per window, not one per cycle -- and a failed or
    wrong-shaped answer never replaces the known-good cache (a login wall that
    returns 200 PARSES to zero features, which a naive reader would persist as
    "no quakes" and go quiet).
    """
    if now - safe_http.num(state.get("last_fetched")) >= _CACHE_TTL_SEC:
        status, data = _fetch(state["http"])
        if status == "ok":
            features = data.get("features") if isinstance(data, dict) else None
            if isinstance(features, list):
                state["events"] = _parse(features)
            else:
                logger.warning("usgs_quakes: 200 but not a FeatureCollection -- "
                               "keeping the cached events")
        state["last_fetched"] = now
        _save_state(state)
    return [e for e in state.get("events", []) if isinstance(e, dict) and e.get("id")]


def _is_promoted(ev: dict) -> bool:
    return ev["mag"] >= _promote_mag() or ev.get("alert") in _PROMOTE_ALERTS


def _urgency(promoted: bool, event_time: float, now: float) -> float:
    fresh, stale = _URGENCY_PROMOTED if promoted else _URGENCY_AMBIENT
    age_h = max(0.0, (now - event_time) / 3600.0) if event_time else 0.0
    return fresh if age_h <= _FRESH_HOURS else stale


def _trigger(ev: dict, promoted: bool, now: float) -> dict:
    place = ev["place"]
    return {
        "type": "usgs_quake",
        # Identity is `id`, so the magnitude in the text may be revised freely.
        "detail": f"M{ev['mag']:.1f} earthquake -- {place}"[:280],
        "id": ev["id"],
        "url": ev["url"],
        "novelty": safe_http.clamp01(0.7),
        "relevance": safe_http.clamp01(_RELEVANCE_PROMOTED if promoted else _RELEVANCE_AMBIENT),
        "urgency": safe_http.clamp01(min(0.6, _urgency(promoted, ev["time"], now))),
        "promoted": promoted,
        "magnitude": ev["mag"],
        "place": place,
        "lat": ev["lat"], "lon": ev["lon"], "depth_km": ev["depth_km"],
        "event_time": ev["time"],
        "pager_alert": ev["alert"],
        "tsunami": ev["tsunami"],
        "source": "usgs",
    }


def scan_usgs_quakes() -> list[dict]:
    """Triggers for earthquakes not yet announced (see module docstring)."""
    now = time.time()
    state = _load_state()
    events = _events(state, now)
    if not events:
        return []  # dark or empty feed: stay cold rather than baseline nothing

    seen = read_seen(_seen_path())
    if seen is None:
        # Cold start: everything on the feed right now is already history.
        baseline: dict[str, float] = {}
        for ev in events:
            baseline[ev["id"]] = now
            if _is_promoted(ev):
                baseline[ev["id"] + "!"] = now
        write_seen(_seen_path(), baseline, _SEEN_MAX)
        return []

    fresh: list[tuple[dict, bool]] = []
    for ev in events:
        promoted = _is_promoted(ev)
        key = ev["id"] + "!" if promoted else ev["id"]
        if key not in seen and not (not promoted and ev["id"] + "!" in seen):
            fresh.append((ev, promoted))

    # Promoted first, then newest: with a hard per-sweep cap, a flood of fresh
    # small quakes must never crowd out the one big one. Items past the cap are
    # left unstamped and fire on a later sweep instead of being swallowed.
    fresh.sort(key=lambda p: (not p[1], -p[0]["time"]))
    triggers: list[dict] = []
    for ev, promoted in fresh[:_MAX_TRIGGERS]:
        seen[ev["id"]] = now
        if promoted:
            seen[ev["id"] + "!"] = now  # a later downward revision must not re-announce
        triggers.append(_trigger(ev, promoted, now))

    if triggers:
        protect = {e["id"] for e in events} | {e["id"] + "!" for e in events}
        write_seen(_seen_path(), seen, _SEEN_MAX, protect=protect)
    return triggers


if __name__ == "__main__":  # manual probe: python -m scanners.world.usgs_quakes
    for t in scan_usgs_quakes()[:5]:
        print(t["detail"], "| promoted" if t["promoted"] else "")
