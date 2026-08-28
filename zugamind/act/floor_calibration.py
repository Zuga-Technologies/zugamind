"""Self-calibrating wake floor — EXP-004t productized (issue #12).

EXP-004t measured that one globally-calibrated `wake_min_salience` floor
(max ambient winner salience observed over a calibration window + a small
margin) reaches 1.03x the cost of S per-source hand-tuned gates at 12
sources, with zero detection loss — because alarm-lane winners bypass the
floor entirely (safe only because of #11's fix). That procedure
(`scripts/run_exp004.py::calibrate_workspace_floor`) ran OFFLINE, against a
held-out calibration corpus, before the measured run.

This is the ONLINE analogue for a live deployment, which has no held-out
corpus to calibrate against: opt in per-harness with
`"wake_min_salience": "calibrate"` in the harness config (instead of a
static number), and this module learns the floor from the live ambient wake
stream itself — same idea, applied cycle by cycle instead of in one offline
pass. The formula is NOT the offline `max + margin` any more: see "Rolling
quantile, not frozen max" below for why it became a 90th-percentile + margin
over a rolling window (2026-08-06).

Warmup safety: before `CALIBRATION_WINDOW` ambient samples have been
observed, `resolve_floor()` returns `WARMUP_FLOOR` — the same 0.35 the
product has always shipped as a static default — so calibrate mode is never
MORE permissive than today's default while it's still learning.

Rolling quantile, not frozen max (redesigned 2026-08-06): the original
design froze `max(20 samples) + margin` forever. Both choices failed live
the same week: `max` let two 0.99 outlier winners recorded as "ambient" set
a 1.04 floor on a 1.0-bounded scale (nothing could ever wake), and freezing
meant the bar could never recover when the environment changed (the same
day, new sensory feeds shifted normal winner salience from ~0.25 to
0.6-0.75). Now: keep the last `ROLLING_WINDOW` ambient samples, floor =
`QUANTILE`th quantile + margin, recomputed on every sample, clamped to
[WARMUP_FLOOR, FLOOR_CEILING]. A quantile barely moves for one outlier; a
rolling window lets the bar drift with reality over ~a day instead of
staying loyal to a stale snapshot.

An "ambient" sample is a winner that reached this harness's wake decision
but was NOT an alarm-lane winner (those bypass the floor by design — they
are not the noise the floor exists to filter) and DID pass the harness's
own `wake_modules` allowlist, if any.

RAW vs MODULATED — which number the gate compares (2026-08-17)
--------------------------------------------------------------
A bid's `salience` is rewritten in place by the attention schema before it
reaches the wake decision: a module gets a x1.2 boost when a DIFFERENT
identity has held attention for 3 cycles, and x1.1 when it is not the
current focus. Those multipliers exist to stop one module monopolising the
mind's INTERNAL attention. Letting them also authorise spending a real
Claude session is a side effect nobody chose. Measured live: a Hacker News
story bid 0.5164 against a 0.655 floor — 0.14 BELOW it — and woke a session
anyway, because 0.5164 x 1.2 x 1.1 = 0.6816.

So the gate should compare `context["raw_salience"]` (what the module
actually asked for) rather than `salience`. The trap: this floor was fitted
on MODULATED samples, so switching the comparison alone silently raises the
effective bar — the same change has to re-fit the floor on raw values, or
the fix is worse than the bug.

Historical samples cannot be converted: the multipliers that produced them
were never journaled, so there is no way to divide them back out. Instead
both series are recorded in parallel and the gate switches basis only once
the RAW series is independently calibrated:

    raw_samples < CALIBRATION_WINDOW  ->  basis "modulated", today's floor,
                                          behaviour byte-for-byte unchanged
    raw_samples >= CALIBRATION_WINDOW ->  basis "raw", floor fitted on raw

There is never a window where a floor fitted on one basis judges the other.
The basis switch is journaled once, as `floor_basis_switched`.

State persisted to `<data_dir>/floor_calibration.json`, keyed by harness
name, written atomically (foundation.fs) and sanitized on load — a torn or
poisoned file used to wedge a harness at its last floor forever (audit
2026-08-28). The first calibration is journaled once (`floor_calibrated`);
after that the floor moves every sample, and each move of at least
`DRIFT_JOURNAL_DELTA` from the last journaled value is journaled as
`floor_drifted` with an `at_ceiling` flag — FLOOR_CEILING is the
"nothing wakes" state and an operator must be able to see it arrive.
Stdlib-only, never raises (mirrors the rest of act/).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from continuity import journal
from foundation.config import DATA_DIR
from foundation.fs import atomic_write_text

logger = logging.getLogger("zugamind.act.floor_calibration")

STATE_FILE = DATA_DIR / "floor_calibration.json"

CALIBRATION_WINDOW = 20   # min samples before a calibrated floor applies
ROLLING_WINDOW = 50       # max samples kept; older ones age out
QUANTILE = 0.9            # floor sits above ~90% of ambient noise, not above all of it
CALIBRATION_MARGIN = 0.05
WARMUP_FLOOR = 0.35
# Hard ceiling on any calibrated floor. Salience is bounded at 1.0, so a
# floor above ~0.9 means "never wake" — the opposite of what calibration is
# for. Found live 2026-08-06: near-max winners (0.99) recorded as "ambient"
# pushed max(samples)+margin to 1.04, silently disabling every wake since
# calibration day. Applied at compute AND resolve so pre-existing bad state
# files heal without manual surgery.
FLOOR_CEILING = 0.9
# A calibrated floor is recomputed on every sample; journal its movement only
# once it has moved this far from the last journaled value.
DRIFT_JOURNAL_DELTA = 0.05


def _quantile_floor(samples: list) -> float:
    """Floor = QUANTILEth quantile of ambient samples + margin, clamped to
    [WARMUP_FLOOR, FLOOR_CEILING]. Conservative nearest-rank quantile —
    stdlib-only, stable for the 20-50 sample sizes this module holds."""
    import math
    ordered = sorted(samples)
    k = max(0, math.ceil(QUANTILE * len(ordered)) - 1)
    raw = ordered[k] + CALIBRATION_MARGIN
    return round(min(max(raw, WARMUP_FLOOR), FLOOR_CEILING), 4)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _sanitize(state: Any) -> Dict[str, Any]:
    """Coerce a loaded state file into the shape the math assumes.

    A non-numeric sample used to raise inside `_quantile_floor` BEFORE
    `_save_state` ran, so the poisoned file was never rewritten and that
    harness stayed wedged at its last floor forever, one warning per cycle
    (audit 2026-08-28). Bad samples are dropped, bad floors reset to None (so
    they are recomputed from the surviving samples), bad entries dropped."""
    if not isinstance(state, dict):
        return {}
    clean: Dict[str, Any] = {}
    for name, entry in state.items():
        if not isinstance(entry, dict):
            logger.warning("floor_calibration: dropping malformed entry for %r", name)
            continue
        e = dict(entry)
        for key in ("samples", "raw_samples"):
            vals = e.get(key)
            if vals is None and key == "raw_samples":
                continue
            good = [float(v) for v in vals if _is_number(v)] if isinstance(vals, list) else []
            if not isinstance(vals, list) or len(good) != len(vals):
                logger.warning("floor_calibration: dropped bad %s for %r", key, name)
            e[key] = good[-ROLLING_WINDOW:]
        for key in ("floor", "raw_floor"):
            v = e.get(key)
            e[key] = float(v) if _is_number(v) else None
        clean[name] = e
    return clean


def _load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return _sanitize(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except Exception as e:  # noqa: BLE001 — a corrupt state file must not crash the caller
        logger.warning("floor_calibration state load failed (non-fatal): %s", e)
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    try:
        atomic_write_text(STATE_FILE, json.dumps(state, indent=2))
    except Exception as e:  # noqa: BLE001 — persistence is best-effort
        logger.warning("floor_calibration state save failed (non-fatal): %s", e)


def _journal_drift(name: str, entry: Dict[str, Any], basis: str) -> None:
    """Journal a calibrated floor's movement once it has moved
    DRIFT_JOURNAL_DELTA from the last value journaled. The floor is recomputed
    on every sample; without this an operator could only see it drift (0.4 ->
    0.9 over a day) by diffing the state file. A floor arriving at
    FLOOR_CEILING is flagged: that is the "nothing wakes" state."""
    key_floor, key_last = ("floor", "journaled_floor") if basis == "modulated" else ("raw_floor", "raw_journaled_floor")
    new, last = entry.get(key_floor), entry.get(key_last)
    if not _is_number(new):
        return
    if not _is_number(last):
        entry[key_last] = new  # pre-existing state from before drift journaling: start tracking silently
        return
    if abs(new - last) < DRIFT_JOURNAL_DELTA:
        return
    entry[key_last] = new
    journal.append_event("floor_drifted", {
        "harness": name, "basis": basis, "from": last, "to": new,
        "at_ceiling": new >= FLOOR_CEILING,
    })


def maybe_record_ambient_sample(hc: Dict[str, Any], winner_dict: Optional[Dict[str, Any]]) -> None:
    """Record this cycle's winner salience as an ambient sample, if `hc` is
    in calibrate mode and this winner qualifies. No-op otherwise. Never
    raises. Call AFTER this cycle's wake decision — calibration state
    updates apply starting next cycle, so a winner can never retroactively
    raise the floor against itself."""
    try:
        if hc.get("wake_min_salience") != "calibrate":
            return
        if winner_dict is None:
            return
        if (winner_dict.get("context") or {}).get("alarm_lane"):
            return  # alarm-lane winners bypass the floor; not ambient noise
        modules = hc.get("wake_modules")
        if isinstance(modules, list) and modules:
            if winner_dict.get("source_module") not in modules:
                return
        salience = winner_dict.get("salience")
        if not isinstance(salience, (int, float)):
            return
        name = hc.get("name")
        if not name:
            return

        state = _load_state()
        entry = state.setdefault(name, {"samples": [], "floor": None, "calibrated_at": None})

        entry["samples"] = (entry["samples"] + [float(salience)])[-ROLLING_WINDOW:]
        if len(entry["samples"]) >= CALIBRATION_WINDOW:
            first_calibration = entry.get("floor") is None
            entry["floor"] = _quantile_floor(entry["samples"])
            if first_calibration:
                entry["calibrated_at"] = journal.now_iso()
                entry["journaled_floor"] = entry["floor"]
                journal.append_event("floor_calibrated", {
                    "harness": name, "floor": entry["floor"],
                    "samples": len(entry["samples"]),
                    "at_ceiling": entry["floor"] >= FLOOR_CEILING,
                })
            else:
                _journal_drift(name, entry, "modulated")

        # Parallel RAW series — see the RAW vs MODULATED section in the module
        # docstring. Deliberately SKIPPED rather than defaulted to `salience`
        # when raw_salience is absent: falling back would seed the raw window
        # with boosted numbers, which is exactly the contamination this series
        # exists to avoid. Absence is transitional (records written before
        # workspace.py started stamping it), so the window fills a little
        # slower rather than filling with the wrong thing.
        raw = (winner_dict.get("context") or {}).get("raw_salience")
        if isinstance(raw, (int, float)):
            entry["raw_samples"] = (entry.get("raw_samples") or [])
            entry["raw_samples"] = (entry["raw_samples"] + [float(raw)])[-ROLLING_WINDOW:]
            if len(entry["raw_samples"]) >= CALIBRATION_WINDOW:
                first_raw = entry.get("raw_floor") is None
                entry["raw_floor"] = _quantile_floor(entry["raw_samples"])
                if first_raw:
                    entry["raw_calibrated_at"] = journal.now_iso()
                    entry["raw_journaled_floor"] = entry["raw_floor"]
                    journal.append_event("floor_basis_switched", {
                        "harness": name,
                        "basis": "raw",
                        "raw_floor": entry["raw_floor"],
                        "previous_modulated_floor": entry.get("floor"),
                        "samples": len(entry["raw_samples"]),
                        "at_ceiling": entry["raw_floor"] >= FLOOR_CEILING,
                        "why": ("the gate now compares what the module asked for, "
                                "not the attention-boosted number"),
                    })
                else:
                    _journal_drift(name, entry, "raw")
        _save_state(state)
    except Exception as e:  # noqa: BLE001 — calibration must never break a wake cycle
        logger.warning("floor_calibration record failed (non-fatal): %s", e)


def resolve_floor(harness_name: str) -> float:
    """The floor only, basis-blind — a convenience over `resolve_gate()`.

    Delegates so it can never again return the MODULATED floor after the
    basis has switched to raw (it did, audit 2026-08-28 — no production
    caller had tripped on it yet). Gate code must use `resolve_gate()`: a
    floor is meaningless without knowing which number it judges. Never
    raises."""
    return resolve_gate(harness_name)[0]


def resolve_gate(harness_name: str) -> tuple:
    """Return `(floor, basis)` — the floor AND which number to compare it to.

    basis "raw"       -> compare `context["raw_salience"]`, what the module
                         actually asked for, against a floor fitted on raw
                         samples.
    basis "modulated" -> compare `salience` against the existing floor, i.e.
                         exactly today's behaviour.

    The basis is whichever series is calibrated; raw wins once it is. A floor
    fitted on one basis is NEVER used to judge the other — that mismatch is
    the whole trap this function exists to avoid. Never raises."""
    try:
        state = _load_state()
        entry = state.get(harness_name) or {}
        raw_floor = entry.get("raw_floor")
        raw_n = len(entry.get("raw_samples") or [])
        if raw_floor is not None and raw_n >= CALIBRATION_WINDOW:
            return min(float(raw_floor), FLOOR_CEILING), "raw"
        if entry.get("floor") is not None:
            return min(float(entry["floor"]), FLOOR_CEILING), "modulated"
    except Exception as e:  # noqa: BLE001
        logger.warning("floor_calibration resolve_gate failed (non-fatal): %s", e)
    return WARMUP_FLOOR, "modulated"
