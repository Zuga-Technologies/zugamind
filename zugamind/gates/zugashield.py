"""ZugaShield — misevolution detector.

The first half of the safety stack: compares current safety metrics to a
captured "birth baseline" and emits drift severity. Acute / per-cycle
detector. The second half (integrity.py) does the slow / longitudinal
ADF stationarity test on the drift history this module produces.

Lifecycle:
  1. capture_birth_baseline — called once after enough data exists (>=20
     predictions). Defines "normal." Append-only, never overwritten.
  2. compute_misevolution_drift — called every cycle. Picks the time-of-day
     regime (active hours 7-14 vs quiet 15-23+0-6) and computes weighted
     drift across 6 safety metrics. Logs to drift_history.jsonl.
  3. handle_misevolution — maps severity to action:
       GREEN  : silent (drift < 0.2)
       YELLOW : warning event + log warning (drift 0.2-0.5)
       RED    : halt event + report written to disk + log error (drift >= 0.5)

     Both YELLOW and RED also invoke the caller-supplied `on_alert` hook, if
     any — the generic human-notification integration point. A deployer wires
     their own Discord/Slack/email delivery there; this module only computes,
     logs, and (at RED) persists a report + halts.

Per-metric drift directionality:
  LOWER_IS_BETTER  (error_rate, calibration_gap, prediction_reality_gap)
                   — only alarm when WORSENING
  HIGHER_IS_BAD    (action_rate, paid_spend_rate, state_transitions_rate)
                   — only alarm when INCREASING beyond baseline
  Individual metric drift capped at 3.0 to prevent one outlier dominating.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Callable, Optional

try:
    from foundation.config import BIRTH_BASELINE_FILE, DATA_DIR, DRIFT_HISTORY_FILE
except ImportError:  # pragma: no cover - fallback if the names aren't defined yet
    from foundation.config import DATA_DIR, ENGINE_DIR  # type: ignore
    BIRTH_BASELINE_FILE = ENGINE_DIR / "birth_baseline.json"
    DRIFT_HISTORY_FILE = ENGINE_DIR / "drift_history.jsonl"

from stream.events import emit_event, ensure_dirs, read_recent_events

logger = logging.getLogger("zugamind.zugashield")

# Drift thresholds
DRIFT_GREEN = 0.2
DRIFT_YELLOW = 0.5
# Above DRIFT_YELLOW = RED

# Safety metric weights
SAFETY_WEIGHTS = {
    "action_rate": 0.20,
    "prediction_reality_gap": 0.20,
    "calibration_gap": 0.15,
    "paid_spend_rate": 0.20,
    "state_transitions_rate": 0.10,
    "error_rate": 0.15,
}


def _calibration_gap(w: dict) -> float:
    """True calibration gap: |avg_confidence - accuracy| over the window.

    Was `1 - accuracy`, which conflated low ACCURACY with miscalibration and
    defaulted to 0.5 on missing data -> a permanent false RED halt (a
    well-calibrated coin-flip predictor scored 0.5). Absent data -> 0.0 (no
    signal), matching the other LOWER_IS_BETTER metrics.
    """
    acc = w.get("accuracy")
    conf = w.get("avg_confidence")
    if acc is None or conf is None:
        return 0.0
    return abs(conf - acc)


def _count_recent_event_rate(event_type: str, last_n_events: int) -> float:
    """Count rate of a specific event type in recent events."""
    events = read_recent_events(last_n_events)
    if not events:
        return 0.0
    count = sum(1 for e in events if e.get("type") == event_type)
    return count / len(events)


def capture_birth_baseline(state: dict, budget: dict, analytics: dict):
    """Capture the birth baseline — called once, then protected.

    This defines "normal" for the agent. All future drift is measured
    against these values. The file is append-only and protected.

    Uses sliding_windows.last_20 instead of overall base_rates to avoid
    contamination from pre-pipeline regime data.

    Stores two time-of-day regimes so evening idle periods are not falsely
    compared against a busy-morning baseline:
      active  — hours 7-14 (peak activity)
      quiet   — hours 15-23 and 0-6 (low activity)
    """
    if BIRTH_BASELINE_FILE.exists():
        return  # Already captured — never overwrite

    windows = analytics.get("sliding_windows", {})
    base = analytics.get("base_rates", {})
    total_preds = analytics.get("total_predictions", 0)

    # Don't capture baseline until we have enough data for stability.
    # A baseline from <20 predictions produces false-positive drift alerts
    # because the metrics haven't converged yet.
    if total_preds < 20:
        return

    # Use recent window metrics instead of all-time to avoid contamination
    # from a pre-pipeline regime.
    recent = windows.get("last_20", windows.get("last_10", {}))

    # Shared point-in-time metrics (not time-of-day dependent)
    paid_spend_rate = budget.get("paid_spent", 0) / max(state.get("cycles_today", 1), 1)
    state_transitions_rate = _count_recent_event_rate("state_transition", 50)
    error_rate = _count_recent_event_rate("cycle_error", 50)

    def _metrics_from_window(w: dict) -> dict:
        return {
            "action_rate": w.get("action_rate", base.get("action_rate", 0.1)),
            "prediction_reality_gap": abs(w.get("prediction_rate", 0) - w.get("action_rate", 0)),
            "calibration_gap": _calibration_gap(w),
            "paid_spend_rate": paid_spend_rate,
            "state_transitions_rate": state_transitions_rate,
            "error_rate": error_rate,
        }

    # Build per-regime action_rate from hourly_rates if available.
    # hourly_rates keys are string hours "0".."23".
    hourly_rates = analytics.get("time_of_day", {}).get("hourly_rates", {})
    ACTIVE_HOURS = {7, 8, 9, 10, 11, 12, 13, 14}
    QUIET_HOURS = {15, 16, 17, 18, 19, 20, 21, 22, 23, 0, 1, 2, 3, 4, 5, 6}

    def _regime_action_rate(hour_set: set):
        """Weighted average action_rate across the given hour set. Returns None if no data."""
        total_samples = 0
        total_acted = 0
        for h in hour_set:
            entry = hourly_rates.get(str(h))
            if entry and entry.get("samples", 0) > 0:
                total_samples += entry["samples"]
                total_acted += entry.get("acted", 0)
        if total_samples == 0:
            return None
        return round(total_acted / total_samples, 4)

    # Base metrics from the recent window (all-hour average)
    base_metrics = _metrics_from_window(recent)

    # Active-regime metrics: override action_rate with active-hours average if available
    active_metrics = dict(base_metrics)
    active_action_rate = _regime_action_rate(ACTIVE_HOURS)
    if active_action_rate is not None:
        active_metrics["action_rate"] = active_action_rate

    # Quiet-regime metrics: override action_rate with quiet-hours average if available
    quiet_metrics = dict(base_metrics)
    quiet_action_rate = _regime_action_rate(QUIET_HOURS)
    if quiet_action_rate is not None:
        quiet_metrics["action_rate"] = quiet_action_rate

    baseline = {
        "captured_at": datetime.now().isoformat(),
        "cycle_count": state.get("cycles_today", 0),
        "total_predictions": total_preds,
        "window_used": "last_20" if "last_20" in windows else "last_10",
        # Top-level "metrics" kept for backward compatibility — equals all-hour base metrics
        "metrics": base_metrics,
        "regimes": {
            "active": {
                "hours": sorted(ACTIVE_HOURS),
                "metrics": active_metrics,
            },
            "quiet": {
                "hours": sorted(QUIET_HOURS),
                "metrics": quiet_metrics,
            },
        },
    }

    ensure_dirs()
    BIRTH_BASELINE_FILE.write_text(json.dumps(baseline, indent=2))
    emit_event("birth_baseline_captured", {"metrics": baseline["metrics"]})
    logger.info(
        "Birth baseline captured (window=%s, %d total predictions, active_rate=%.3f, quiet_rate=%.3f)",
        baseline["window_used"],
        total_preds,
        active_metrics["action_rate"],
        quiet_metrics["action_rate"],
    )


def compute_misevolution_drift(state: dict, budget: dict, analytics: dict) -> dict:
    """Compare current safety metrics to birth baseline. Returns drift report.

    Drift = weighted average of per-metric deviations from baseline.
    GREEN (<0.2): healthy. YELLOW (0.2-0.5): monitoring. RED (>0.5): halt.

    Picks the time-of-day-aware regime baseline when available:
      active  (hours 7-14)  — compared against active-regime metrics
      quiet   (hours 15-23, 0-6) — compared against quiet-regime metrics
    Falls back to the legacy top-level "metrics" key for old baseline files.
    """
    if not BIRTH_BASELINE_FILE.exists():
        return {"severity": "GREEN", "drift": 0.0, "detail": "No baseline yet"}

    try:
        baseline = json.loads(BIRTH_BASELINE_FILE.read_text())
    except (json.JSONDecodeError, Exception):
        return {"severity": "GREEN", "drift": 0.0, "detail": "Baseline unreadable"}

    # Select regime-aware baseline metrics based on current hour.
    # Hours 7-14 are "active"; everything else is "quiet".
    # If the baseline file pre-dates regime support, fall back to top-level "metrics".
    current_hour = datetime.now().hour
    regimes = baseline.get("regimes", {})
    if regimes:
        if 7 <= current_hour <= 14:
            regime_name = "active"
        else:
            regime_name = "quiet"
        birth = regimes.get(regime_name, {}).get("metrics") or baseline.get("metrics", {})
    else:
        # Old baseline file — no regime data, use legacy top-level metrics
        regime_name = "legacy"
        birth = baseline.get("metrics", {})
    cycles_today = max(state.get("cycles_today", 1), 1)

    # Use same window as baseline to avoid comparing clean baseline
    # against contaminated all-time metrics
    windows = analytics.get("sliding_windows", {})
    recent = windows.get("last_20", windows.get("last_10", {}))

    # Compute current values from recent window (matches baseline source)
    current = {
        "action_rate": recent.get("action_rate", analytics.get("base_rates", {}).get("action_rate", 0)),
        "prediction_reality_gap": abs(recent.get("prediction_rate", 0) - recent.get("action_rate", 0)),
        "calibration_gap": _calibration_gap(recent),
        "paid_spend_rate": budget.get("paid_spent", 0) / cycles_today,
        "state_transitions_rate": _count_recent_event_rate("state_transition", 50),
        "error_rate": _count_recent_event_rate("cycle_error", 50),
    }

    # Compute per-metric drift. Only alarm on WORSENING metrics.
    # "lower is better" metrics: a decrease from baseline is safe.
    # "stable is better" metrics: any large change is concerning.
    LOWER_IS_BETTER = {"error_rate", "calibration_gap", "prediction_reality_gap"}
    HIGHER_IS_BAD = {"action_rate", "paid_spend_rate", "state_transitions_rate"}

    per_metric = {}
    weighted_drift = 0.0
    for metric, weight in SAFETY_WEIGHTS.items():
        birth_val = birth.get(metric, 0)
        curr_val = current.get(metric, 0)
        denominator = max(abs(birth_val), 0.01)

        if metric in LOWER_IS_BETTER:
            # Only drift if WORSENING (current > birth)
            raw_drift = max(0, (curr_val - birth_val)) / denominator
        elif metric in HIGHER_IS_BAD:
            # Only drift if INCREASING beyond baseline
            raw_drift = max(0, (curr_val - birth_val)) / denominator
        else:
            # Absolute deviation for unknown metrics
            raw_drift = abs(curr_val - birth_val) / denominator

        # Cap individual metric drift at 3.0 to prevent one outlier dominating
        capped_drift = min(raw_drift, 3.0)
        per_metric[metric] = {
            "birth": round(birth_val, 4),
            "current": round(curr_val, 4),
            "drift": round(capped_drift, 4),
        }
        weighted_drift += weight * capped_drift

    drift = round(weighted_drift, 4)

    # Determine severity
    if drift >= DRIFT_YELLOW:
        severity = "RED"
    elif drift >= DRIFT_GREEN:
        severity = "YELLOW"
    else:
        severity = "GREEN"

    report = {
        "timestamp": datetime.now().isoformat(),
        "severity": severity,
        "drift": drift,
        "regime": regime_name,  # "active", "quiet", or "legacy" (old baseline)
        "per_metric": per_metric,
        "thresholds": {"green": DRIFT_GREEN, "yellow": DRIFT_YELLOW},
    }

    # Log to drift history
    ensure_dirs()
    with open(DRIFT_HISTORY_FILE, "a") as f:
        f.write(
            json.dumps(
                {
                    "ts": datetime.now().isoformat(),
                    "drift": drift,
                    "severity": severity,
                    "regime": regime_name,
                }
            )
            + "\n"
        )

    return report


def handle_misevolution(report: dict, on_alert: Optional[Callable[[str, dict], None]] = None):
    """Take action based on drift severity.

    `on_alert(severity, report)` is an optional notification hook — the
    generic human-veto / notification integration point. A deployer wires
    their own Discord/Slack/email delivery in here; the core gate itself only
    logs, and at RED, persists a report to disk and halts.
    """
    severity = report.get("severity", "GREEN")

    if severity == "GREEN":
        return

    drift = report.get("drift", 0)
    per_metric = report.get("per_metric", {})

    # Find the top drifting metrics for the alert message
    top_drifters = sorted(per_metric.items(), key=lambda x: x[1].get("drift", 0), reverse=True)[:3]
    detail_lines = [
        f"{name}: {d['birth']:.3f} -> {d['current']:.3f} (drift={d['drift']:.2f})" for name, d in top_drifters
    ]
    detail = "; ".join(detail_lines)

    if severity == "YELLOW":
        emit_event(
            "misevolution_warning",
            {
                "drift": drift,
                "detail": detail,
            },
        )
        logger.warning("MISEVOLUTION WARNING: drift=%.3f — %s", drift, detail)
        if on_alert is not None:
            try:
                on_alert("YELLOW", report)
            except Exception as exc:
                logger.debug("zugashield on_alert (YELLOW) failed: %s", exc)

    elif severity == "RED":
        emit_event(
            "misevolution_halt",
            {
                "drift": drift,
                "detail": detail,
            },
        )
        logger.error("MISEVOLUTION HALT: drift=%.3f — %s", drift, detail)

        # ACTUAL HALT: RED must stop the loop, not just alert. Write a PAUSE
        # file the run loop checks at cycle start. Fail-toward-halt: a false
        # RED is recoverable (remove the file); a missed RED during real
        # misevolution is not.
        try:
            from foundation.config import PAUSE_FILE
            _msg = (
                "ZUGASHIELD RED HALT " + datetime.now().isoformat()
                + " drift=" + ("%.3f" % drift) + "\n" + str(detail) + "\n"
                + "Misevolution drift >= 0.5. Review the misevolution report, "
                + "then remove this PAUSE file to resume."
            )
            PAUSE_FILE.write_text(_msg)
            logger.error("ZugaShield RED -> PAUSE file written; cognitive loop halts next cycle")
        except Exception as exc:
            logger.error("ZugaShield RED could not write PAUSE file: %s", exc)

        # Write a report to disk for human review.
        try:
            reports_dir = DATA_DIR / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            report_path = reports_dir / f"misevolution-{datetime.now().strftime('%Y-%m-%d-%H%M')}.md"
            report_content = f"# MISEVOLUTION DETECTED — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            report_content += f"**Drift score:** {drift:.3f} (RED threshold: {DRIFT_YELLOW})\n\n"
            report_content += "**Metrics:**\n"
            for name, d in top_drifters:
                report_content += f"- {name}: {d['birth']:.4f} -> {d['current']:.4f} (drift {d['drift']:.2f})\n"
            report_content += "\n**Action:** Experiment engine paused. Human review required.\n"
            report_path.write_text(report_content)
        except Exception as exc:
            logger.error("zugashield: failed to write RED report: %s", exc)

        if on_alert is not None:
            try:
                on_alert("RED", report)
            except Exception as exc:
                logger.debug("zugashield on_alert (RED) failed: %s", exc)
