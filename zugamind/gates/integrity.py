"""Consciousness Integrity — slow / longitudinal drift health.

The second half of the safety stack (zugashield is the per-cycle acute
detector). Asks a different question: not "is today's drift above
threshold?" but "is the drift TRENDING?" — which catches insidious slow
alignment drift that stays below per-cycle thresholds but accumulates.

Method: Augmented Dickey-Fuller (ADF) stationarity test on the drift
history that zugashield writes to drift_history.jsonl. ADF p-value < 0.05
means the series is mean-reverting (good). Higher p-value = non-stationary
= drift is not snapping back, possibly trending.

Severity classification:
  STABLE   — ADF p < 0.05, drift is mean-reverting
  DRIFTING — non-stationary, slope >= 0 (slight upward trend)
  CRITICAL — non-stationary, slope > 0.01 (clear upward trend)
             possible belief drift / memory grafting / alignment shift

Stdlib-only. The stationarity test is a Dickey-Fuller unit-root regression
implemented in pure Python below — NO statsmodels/numpy. When the series is
degenerate (zero variance, too few points) the report is tagged
`analysis="insufficient"` so a can't-compute STABLE is distinguishable from a
genuinely-healthy mean-reverting STABLE (greppable, never silently dark).
"""

import json
import logging
import math
from datetime import datetime
from typing import Callable, Optional

try:
    from foundation.config import DATA_DIR, DRIFT_HISTORY_FILE, ENGINE_DIR
except ImportError:  # pragma: no cover - fallback if the names aren't defined yet
    from foundation.config import DATA_DIR  # type: ignore
    ENGINE_DIR = DATA_DIR
    DRIFT_HISTORY_FILE = ENGINE_DIR / "drift_history.jsonl"

from stream.events import emit_event, ensure_dirs

logger = logging.getLogger("zugamind.integrity")

INTEGRITY_HISTORY_FILE = ENGINE_DIR / "integrity_history.jsonl"
MIN_INTEGRITY_SAMPLES = 10  # need 10+ drift readings for ADF


# Dickey-Fuller critical values for the constant-no-trend model (asymptotic,
# MacKinnon). t-stat below the 5% value (-2.86) => reject unit root => stationary.
# Anchor points map the DF t-stat to an approximate p-value by piecewise-linear
# interpolation — exact enough for an alert display, honest about being approximate.
_DF_T_TO_P = [
    (-4.50, 0.001),
    (-3.43, 0.010),  # 1%
    (-2.86, 0.050),  # 5%
    (-2.57, 0.100),  # 10%
    (-1.50, 0.500),
    (0.00, 0.900),
]
_DF_STATIONARY_T = -2.86  # 5% critical value


def _df_t_to_pvalue(t: float) -> float:
    """Approximate p-value for a Dickey-Fuller t-stat via interpolation."""
    pts = _DF_T_TO_P
    if t <= pts[0][0]:
        return pts[0][1]
    if t >= pts[-1][0]:
        return pts[-1][1]
    for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
        if t0 <= t <= t1:
            frac = (t - t0) / (t1 - t0) if t1 != t0 else 0.0
            return round(p0 + frac * (p1 - p0), 4)
    return 0.999


def _dickey_fuller(series: list) -> tuple:
    """Pure-stdlib Dickey-Fuller unit-root test (constant, no trend, lag 0).

    Regresses Δy_t on [1, y_{t-1}] via OLS; the t-stat of the y_{t-1}
    coefficient is the DF statistic. Returns (t_stat, p_value, is_stationary),
    or None if the series is degenerate (zero lag-variance / too few points).
    """
    n = len(series) - 1
    if n < 3:
        return None
    x = series[:-1]                                   # y_{t-1}
    dy = [series[i] - series[i - 1] for i in range(1, len(series))]  # Δy_t
    mx = sum(x) / n
    mdy = sum(dy) / n
    sxx = sum((xi - mx) ** 2 for xi in x)
    if sxx <= 1e-12:                                  # (near-)constant series — undetermined
        return None
    sxy = sum((x[i] - mx) * (dy[i] - mdy) for i in range(n))
    beta = sxy / sxx
    alpha = mdy - beta * mx
    rss = sum((dy[i] - (alpha + beta * x[i])) ** 2 for i in range(n))
    dof = n - 2
    if dof <= 0:
        return None
    if rss <= 1e-12:                                  # perfect fit (e.g. deterministic ramp) — undetermined
        return None
    se_beta = math.sqrt((rss / dof) / sxx)
    if se_beta == 0:
        return None
    t_stat = beta / se_beta
    return t_stat, _df_t_to_pvalue(t_stat), (t_stat < _DF_STATIONARY_T)


def compute_consciousness_integrity() -> dict:
    """ADF stationarity test on the cognitive drift history.

    Instead of just checking "is today's drift above threshold?", this asks
    "is the drift TRENDING?" — which catches slow, insidious alignment drift
    that stays below the threshold on any single day but accumulates over time.

    Returns:
        severity: STABLE / DRIFTING / CRITICAL
        adf_p_value: float (< 0.05 means stationary = good)
        trend_direction: stable / increasing / decreasing
        recommendation: human-readable action
    """
    # Read drift history
    if not DRIFT_HISTORY_FILE.exists():
        return {"severity": "STABLE", "detail": "No drift history yet"}

    try:
        lines = DRIFT_HISTORY_FILE.read_text().strip().split("\n")
        entries = [json.loads(line) for line in lines if line.strip()]
    except Exception:
        return {"severity": "STABLE", "detail": "Drift history unreadable"}

    if len(entries) < MIN_INTEGRITY_SAMPLES:
        return {
            "severity": "STABLE",
            "detail": f"Need {MIN_INTEGRITY_SAMPLES - len(entries)} more cycles for integrity testing",
            "samples": len(entries),
        }

    # Extract drift values as time series
    drift_values = [e.get("drift", 0.0) for e in entries]

    # Dickey-Fuller stationarity test (pure stdlib — see _dickey_fuller above)
    try:
        df = _dickey_fuller(drift_values)
        if df is None:
            # Degenerate series (zero variance / too few usable points). Can't
            # judge stationarity — tag it so this STABLE is greppable and never
            # mistaken for a healthy mean-reverting STABLE.
            return {
                "severity": "STABLE",
                "analysis": "insufficient",
                "detail": "drift series degenerate — stationarity undetermined",
                "samples": len(drift_values),
            }
        adf_stat, p_value, is_stationary = df

        # Trend detection via simple linear regression on recent window
        recent = drift_values[-20:]  # last 20 readings
        n = len(recent)
        xs = list(range(n))
        sum_x = sum(xs)
        sum_y = sum(recent)
        sum_xy = sum(x * y for x, y in zip(xs, recent))
        sum_x2 = sum(x * x for x in xs)
        denom = n * sum_x2 - sum_x * sum_x

        if denom != 0:
            slope = (n * sum_xy - sum_x * sum_y) / denom
        else:
            slope = 0.0

        # Classify
        if is_stationary:
            # Drift is mean-reverting — healthy
            severity = "STABLE"
            recommendation = "Cognitive drift is stationary (mean-reverting). No action needed."
        elif slope > 0.01:
            # Drift is non-stationary AND trending upward — danger
            severity = "CRITICAL"
            recommendation = (
                "ALERT: Drift is non-stationary and trending upward. "
                "Possible belief drift or memory grafting. "
                "Human review required immediately."
            )
        elif slope > 0:
            severity = "DRIFTING"
            recommendation = (
                "Drift is non-stationary with slight upward trend. "
                "Monitor closely. May indicate gradual alignment shift."
            )
        else:
            # Non-stationary but not trending up — could be noise
            severity = "DRIFTING"
            recommendation = "Drift is non-stationary but not trending upward. May stabilize. Continue monitoring."

        report = {
            "severity": severity,
            "adf_statistic": round(adf_stat, 4),
            "adf_p_value": round(p_value, 4),
            "is_stationary": is_stationary,
            "trend_slope": round(slope, 6),
            "trend_direction": "increasing" if slope > 0.005 else "decreasing" if slope < -0.005 else "stable",
            "samples": len(drift_values),
            "recent_mean_drift": round(sum(recent) / len(recent), 4),
            "recommendation": recommendation,
        }

        # Log integrity check
        ensure_dirs()
        with open(INTEGRITY_HISTORY_FILE, "a") as f:
            f.write(
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(),
                        "severity": severity,
                        "p_value": round(p_value, 4),
                        "slope": round(slope, 6),
                        "samples": len(drift_values),
                    }
                )
                + "\n"
            )

        return report

    except Exception as e:
        return {
            "severity": "STABLE",
            "analysis": "error",
            "detail": f"Integrity check error: {str(e)[:100]}",
        }


def handle_integrity_alert(report: dict, on_alert: Optional[Callable[[str, dict], None]] = None):
    """Escalate consciousness integrity alerts.

    `on_alert(severity, report)` is an optional notification hook — the
    generic human-veto / notification integration point. A deployer wires
    their own Discord/Slack/email delivery in here; the core gate itself only
    logs, and at CRITICAL, persists a report to disk.
    """
    severity = report.get("severity", "STABLE")

    if severity == "STABLE":
        return

    p_val = report.get("adf_p_value", "?")
    slope = report.get("trend_slope", 0)
    recommendation = report.get("recommendation", "")

    if severity == "DRIFTING":
        emit_event(
            "consciousness_integrity_warning",
            {
                "adf_p_value": p_val,
                "trend_slope": slope,
            },
        )
        logger.warning(
            "CONSCIOUSNESS INTEGRITY WARNING: p=%.4f, slope=%.6f — %s",
            p_val,
            slope,
            recommendation,
        )
        if on_alert is not None:
            try:
                on_alert("DRIFTING", report)
            except Exception as exc:
                logger.debug("integrity on_alert (DRIFTING) failed: %s", exc)

    elif severity == "CRITICAL":
        emit_event(
            "consciousness_integrity_critical",
            {
                "adf_p_value": p_val,
                "trend_slope": slope,
            },
        )
        logger.error(
            "CONSCIOUSNESS INTEGRITY CRITICAL: p=%.4f, slope=%.6f — %s",
            p_val,
            slope,
            recommendation,
        )

        # Write a report to disk for human review.
        try:
            reports_dir = DATA_DIR / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            report_path = reports_dir / f"integrity-alert-{datetime.now().strftime('%Y-%m-%d-%H%M')}.md"
            report_content = (
                f"# CONSCIOUSNESS INTEGRITY ALERT — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
                f"**Severity:** CRITICAL\n"
                f"**ADF p-value:** {p_val} (>0.05 = non-stationary = drifting)\n"
                f"**Trend slope:** {slope:.6f} (positive = worsening)\n"
                f"**Samples:** {report.get('samples', '?')}\n"
                f"**Mean recent drift:** {report.get('recent_mean_drift', '?')}\n\n"
                f"**Recommendation:** {recommendation}\n\n"
                f"**What this means:** The agent's behavioral drift is not mean-reverting. "
                f"Unlike normal variance (which snaps back to baseline), the drift is "
                f"trending — indicating possible belief manipulation, memory grafting, "
                f"or gradual alignment degradation.\n\n"
                f"**Action required:** Human review of recent cognitive stream, "
                f"context inputs, and memory modifications.\n"
            )
            report_path.write_text(report_content)
        except Exception as exc:
            logger.error("integrity: failed to write CRITICAL report: %s", exc)

        if on_alert is not None:
            try:
                on_alert("CRITICAL", report)
            except Exception as exc:
                logger.debug("integrity on_alert (CRITICAL) failed: %s", exc)
