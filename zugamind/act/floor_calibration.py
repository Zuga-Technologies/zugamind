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
stream itself — same formula (max + margin), applied cycle by cycle instead
of in one offline pass.

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
name. The floor-change is journaled exactly once, when the window fills —
not every cycle. Stdlib-only, never raises (mirrors the rest of act/).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from continuity import journal
from foundation.config import DATA_DIR

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


def _quantile_floor(samples: list) -> float:
    """Floor = QUANTILEth quantile of ambient samples + margin, clamped to
    [WARMUP_FLOOR, FLOOR_CEILING]. Conservative nearest-rank quantile —
    stdlib-only, stable for the 20-50 sample sizes this module holds."""
    import math
    ordered = sorted(samples)
    k = max(0, math.ceil(QUANTILE * len(ordered)) - 1)
    raw = ordered[k] + CALIBRATION_MARGIN
    return round(min(max(raw, WARMUP_FLOOR), FLOOR_CEILING), 4)


def _load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 — a corrupt state file must not crash the caller
        logger.warning("floor_calibration state load failed (non-fatal): %s", e)
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001 — persistence is best-effort
        logger.warning("floor_calibration state save failed (non-fatal): %s", e)


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
                entry["calibrated_at"] = datetime.now().isoformat()
                journal.append_event("floor_calibrated", {
                    "harness": name, "floor": entry["floor"],
                    "samples": len(entry["samples"]),
                })

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
                    entry["raw_calibrated_at"] = datetime.now().isoformat()
                    journal.append_event("floor_basis_switched", {
                        "harness": name,
                        "basis": "raw",
                        "raw_floor": entry["raw_floor"],
                        "previous_modulated_floor": entry.get("floor"),
                        "samples": len(entry["raw_samples"]),
                        "why": ("the gate now compares what the module asked for, "
                                "not the attention-boosted number"),
                    })
        _save_state(state)
    except Exception as e:  # noqa: BLE001 — calibration must never break a wake cycle
        logger.warning("floor_calibration record failed (non-fatal): %s", e)


def resolve_floor(harness_name: str) -> float:
    """Return this harness's calibrated floor, or WARMUP_FLOOR if it hasn't
    finished calibrating yet. Never raises."""
    try:
        state = _load_state()
        entry = state.get(harness_name)
        if entry and entry.get("floor") is not None:
            return min(float(entry["floor"]), FLOOR_CEILING)
    except Exception as e:  # noqa: BLE001
        logger.warning("floor_calibration resolve failed (non-fatal): %s", e)
    return WARMUP_FLOOR


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
