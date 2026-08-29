"""SourceScheduler — per-source cadence + yield ledger for the perception layer.

This module is the seam: a thin scheduler that gives each scanner two
independent dials —

  * cadence    — min interval between polls (cheap sources poll often, expensive
                 ones rarely); a source is only swept when *due*.
  * yield weight — a rolling novel-signal rate per source, used to bias the
                 per-source emit budget and drive backoff for dead sources.

Poll-all is the default: `due_sources()` returns every registered source every
cycle unless ZUGAMIND_SOURCE_SCHEDULER_ENABLED is truthy. Cadence gating and
the yield-weighted budget only engage behind that flag, so importing/using
this module is a no-op behavior change until the flag flips.

Hard rules honored (see zugamind/CLAUDE.md):
  * stdlib-only — json + os + time + dataclasses, no pip.
  * fail-closed — any scheduler error falls back to poll-all (today's behavior);
    perception must never go dark on a scheduler bug.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from scanners.seen_items import atomic_write_text

logger = logging.getLogger("zugamind.scanners.scheduler")

# Rolling window (in polls) over which yield_rate is computed.
_YIELD_WINDOW = 20

# Default dials when a source isn't named in _STATIC_SPECS.
_DEFAULT_BASE_CADENCE = 300      # 5 min — one sentinel cycle's worth
_DEFAULT_MAX_CADENCE = 6 * 3600  # 6h backoff ceiling

# Default per-source emit quota (mirrors ZUGAMIND_PER_SCANNER_CAP). The yield-weighted
# budget flexes around this; the global ceiling bounds the total so the GWT auction
# can't be flooded by one high-yield source.
def _int_env(name: str, default: int) -> int:
    """Read an int dial tolerantly, at call time.

    These were bare `int(os.environ.get(...))` at MODULE level, so an empty or
    mistyped value raised ValueError during import — and stream.runner imports
    this at module scope, so one bad character in a tuning dial stopped the
    whole agent from booting (.env.example invites exactly this shape). A
    misconfigured dial should degrade to its default, not take the process
    down. `_flag_enabled()` below already got this right; these two were the
    outliers.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("scheduler: %s=%r is not an integer — using %d",
                       name, raw, default)
        return default


def _default_emit_cap() -> int:
    return _int_env("ZUGAMIND_PER_SCANNER_CAP", 3)


def _global_emit_ceiling() -> int:
    return _int_env("ZUGAMIND_GLOBAL_TRIGGER_CEILING", 40)

# Honors ZUGAMIND_DATA_DIR without importing foundation — scanners stay standalone.
#
# Resolved per call, not at import. As a module constant this ignored a
# ZUGAMIND_DATA_DIR set after import, so a test or a script that isolated its
# data dir still wrote the LIVE deployment's ledger — while every other
# scanner in the package (reddit_ai._cache_path, ai_labs._cache_file) resolves
# its own path per call and was correctly isolated. Kept as a module-level
# name too, because tests monkeypatch _LEDGER_PATH directly.
def _data_dir() -> Path:
    return Path(os.environ.get("ZUGAMIND_DATA_DIR")
                or Path(__file__).resolve().parent.parent / "data")


def _default_ledger_path() -> Path:
    return _data_dir() / "scanner_cache" / "_source_ledger.json"


def _ledger_path() -> Path:
    """The live ledger path. Honours a _LEDGER_PATH monkeypatch when a test
    has set one, so the existing test seam keeps working.

    The default is COMPUTED, not stored: a stored one is a module attribute
    holding a path into the live data dir, which is exactly the shape
    tests/foundation/test_no_live_data_writes.py exists to forbid -- and a
    constant that only ever gets compared against is not worth an exception
    to that rule.
    """
    patched = globals().get("_LEDGER_PATH")
    if patched is not None and patched != _default_ledger_path():
        return patched
    return _default_ledger_path()


_DATA_DIR = _data_dir()
_LEDGER_PATH = _default_ledger_path()


def _flag_enabled() -> bool:
    """Cadence gating + yield budget are off until this flag is truthy."""
    return os.environ.get("ZUGAMIND_SOURCE_SCHEDULER_ENABLED", "").lower() in ("1", "true", "yes", "on")


@dataclass
class SourceSpec:
    """One scanner's scheduling profile. `fn` may be None until wired."""
    name: str
    fn: Optional[Callable] = None
    base_cadence_secs: int = _DEFAULT_BASE_CADENCE
    max_cadence_secs: int = _DEFAULT_MAX_CADENCE
    cost_class: str = "http"          # "cheap" | "http" | "heavy"
    always_on: bool = False           # True bypasses cadence entirely


# Static cadence table. Sources not listed get the defaults above.
# always_on covers failure-critical sources that must never be cadence-delayed.
# This trimmed release ships only world-facing HTTP sources, so cadence gating
# is the whole point — wasted round-trips to external services actually cost
# something, unlike local/structural scanners.
_STATIC_SPECS: dict[str, SourceSpec] = {
    "scan_hackernews":  SourceSpec("scan_hackernews",  base_cadence_secs=300,  cost_class="heavy"),
    "scan_reddit_ai":   SourceSpec("scan_reddit_ai",   base_cadence_secs=1800, cost_class="http"),
    "scan_ai_labs":     SourceSpec("scan_ai_labs",     base_cadence_secs=1800, cost_class="http"),
}


@dataclass
class _Ledger:
    """Persisted per-source state: last_polled + rolling (polled, novel) counts."""
    last_polled: dict[str, float] = field(default_factory=dict)
    # name -> list of novel-count ints, most-recent last, capped at _YIELD_WINDOW
    yields: dict[str, list[int]] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"last_polled": self.last_polled, "yields": self.yields}

    @classmethod
    def from_json(cls, raw: dict) -> "_Ledger":
        return cls(
            last_polled=dict(raw.get("last_polled") or {}),
            yields={k: list(v) for k, v in (raw.get("yields") or {}).items()},
        )


def _load_ledger() -> _Ledger:
    try:
        path = _ledger_path()
        if path.exists():
            return _Ledger.from_json(json.loads(path.read_text("utf-8")))
    except Exception as e:  # fail-closed: a corrupt ledger starts fresh, never crashes
        logger.debug("source ledger load failed (starting fresh): %s", e)
    return _Ledger()


def _save_ledger(ledger: _Ledger) -> None:
    try:
        atomic_write_text(_ledger_path(), json.dumps(ledger.to_json(), indent=2))
    except Exception as e:  # persistence is best-effort; never break the cycle
        logger.debug("source ledger save failed (non-fatal): %s", e)


class SourceScheduler:
    """Registry + cadence gate + yield ledger. Singleton via get_scheduler()."""

    def __init__(self, specs: Optional[dict[str, SourceSpec]] = None):
        self.specs: dict[str, SourceSpec] = dict(specs or _STATIC_SPECS)
        self.ledger = _load_ledger()
        # Sources actually polled this cycle (set by note_polled). Lets the yield
        # recorder attribute novel=0 to a source that polled but surfaced nothing
        # — the empty-source case the raw-list approach couldn't see.
        self._polled_this_cycle: set[str] = set()

    # ---- registry ----------------------------------------------------------
    def register(self, spec: SourceSpec) -> None:
        """Add/override a source. Idempotent; dynamic scanners register here."""
        self.specs[spec.name] = spec

    def spec_for(self, name: str) -> SourceSpec:
        return self.specs.get(name) or SourceSpec(name)

    # ---- cadence gate ------------------------------------------------------
    def effective_cadence(self, name: str) -> int:
        """base_cadence, lengthened to max_cadence when a source is sustained-dead.

        Rule-based backoff (no ML): a source whose full rolling window surfaced
        zero novel signal polls at its max_cadence floor — slowed, never silenced.
        Any single novel hit drops it straight back to base_cadence, so a
        re-emerging source recovers next window.
        """
        spec = self.spec_for(name)
        base = spec.base_cadence_secs
        window = self.ledger.yields.get(name) or []
        if len(window) >= _YIELD_WINDOW and not any(n > 0 for n in window):
            return spec.max_cadence_secs
        return base

    def due(self, name: str, now: float) -> bool:
        """True if this source should be swept this cycle.

        always_on bypasses the gate. With the flag off, everything is due
        (poll-all = today's behavior).
        """
        spec = self.spec_for(name)
        if spec.always_on or not _flag_enabled():
            return True
        last = self.ledger.last_polled.get(name)
        if last is None:
            return True
        age = now - last
        if age < 0:
            # A stamp in the FUTURE — a clock step, a timezone slip, another
            # writer's units. `age >= cadence` is then false for as long as the
            # stamp leads the clock, so the source is silently blocked, and the
            # stamp persists on disk across restarts. Measured 2026-08-29: a
            # year-ahead stamp still blocked the source six months later, with
            # no log line at any level. Treat it as due and say so.
            logger.warning(
                "scheduler: %s last_polled is %.0fs in the FUTURE — clock "
                "artefact; treating as due", name, -age,
            )
            return True
        return age >= self.effective_cadence(name)

    def due_sources(self, now: Optional[float] = None) -> list[SourceSpec]:
        """The specs whose sources are due this cycle.

        Fail-closed: any error returns ALL specs (poll-all), never fewer.
        """
        try:
            t = time.time() if now is None else now
            return [s for s in self.specs.values() if self.due(s.name, t)]
        except Exception as e:
            logger.debug("due_sources errored, falling back to poll-all: %s", e)
            return list(self.specs.values())

    # ---- per-cycle polled tracking ------------------------------------------
    def start_cycle(self) -> None:
        """Reset the polled-this-cycle set. Called at the top of the collect step."""
        self._polled_this_cycle = set()

    def note_polled(self, name: str) -> None:
        """Mark that `name` was actually swept this cycle (drives empty-source yield)."""
        self._polled_this_cycle.add(name)

    def polled_this_cycle(self) -> set[str]:
        return set(self._polled_this_cycle)

    # ---- yield-weighted emit budget -----------------------------------------
    def emit_budget(self, name: str, base_cap: "int | None" = None) -> int:
        """Per-source emit quota, flexed by rolling yield. >=1 always (never mute).

        High-yield sources earn one extra slot; a source dead for the full window
        is squeezed to a single slot. Mid sources keep the base cap. The global
        ceiling still bounds the total -- EXCEPT that as of the 2026-08-29
        audit neither this method nor _global_emit_ceiling() has any
        caller in the runner, so nothing bounds the per-cycle total
        today. Kept and tested so it can be wired; the claim is
        corrected here rather than left reading as a live guarantee.
        """
        if base_cap is None:
            base_cap = _default_emit_cap()
        window = self.ledger.yields.get(name) or []
        rate = self.yield_rate(name)
        full = len(window) >= _YIELD_WINDOW
        if full and rate <= 0.0:
            return 1
        if rate >= 0.6:
            return base_cap + 1
        if full and rate < 0.3:
            return max(1, base_cap - 1)
        return base_cap

    # ---- yield ledger --------------------------------------------------------
    def record_yield(self, name: str, novel: int, now: Optional[float] = None) -> None:
        """Record one poll of `name` that surfaced `novel` habituation-survivors.

        Stamps last_polled and appends to the rolling window. Persists best-effort.
        """
        try:
            t = time.time() if now is None else now
            self.ledger.last_polled[name] = t
            window = self.ledger.yields.setdefault(name, [])
            window.append(int(novel))
            if len(window) > _YIELD_WINDOW:
                del window[: len(window) - _YIELD_WINDOW]
            _save_ledger(self.ledger)
        except Exception as e:
            logger.debug("record_yield failed for %s (non-fatal): %s", name, e)

    def yield_rate(self, name: str) -> float:
        """novel / polled over the rolling window. 1.0 when no data (optimistic)."""
        window = self.ledger.yields.get(name) or []
        if not window:
            return 1.0
        polled = len(window)
        novel = sum(1 for n in window if n > 0)
        return novel / polled if polled else 1.0


_SCHEDULER: Optional[SourceScheduler] = None


def get_scheduler() -> SourceScheduler:
    """Process-wide singleton. Dynamic scanners are registered on first build."""
    global _SCHEDULER
    if _SCHEDULER is None:
        sched = SourceScheduler()
        try:
            from . import discover_dynamic_scanners
            for fn_name, fn in discover_dynamic_scanners().items():
                if fn_name not in sched.specs:
                    sched.register(SourceSpec(fn_name, fn=fn))
        except Exception as e:  # discovery failure must not block scheduler use
            logger.debug("dynamic scanner registration failed (non-fatal): %s", e)
        _SCHEDULER = sched
    return _SCHEDULER
