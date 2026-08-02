"""Consciousness Integrity — slow / longitudinal drift health.

Asks a different question than a per-cycle threshold check: not "is today's
drift above threshold?" but "is the drift TRENDING?" — which catches
insidious slow alignment drift that stays below any single-cycle threshold
but accumulates over time.

Method: Augmented Dickey-Fuller (ADF) stationarity test on a drift-value
time series. ADF p-value < 0.05 means the series is mean-reverting (good).
Higher p-value = non-stationary = drift is not snapping back, possibly
trending.

Severity classification:
  STABLE   — ADF p < 0.05, drift is mean-reverting
  DRIFTING — non-stationary, slope >= 0 (slight upward trend)
  CRITICAL — non-stationary, slope > 0.01 (clear upward trend)

Stdlib-only (zugamind hard rule #1) — the stationarity test is a
Dickey-Fuller unit-root regression implemented in pure Python below, no
numpy/statsmodels. When the series is degenerate (zero variance, too few
points) the report is tagged `analysis="insufficient"` so a can't-compute
STABLE is distinguishable from a genuinely-healthy mean-reverting STABLE.

Opt-in library gate: does no I/O and posts no alerts. The caller sources
`drift_values` from their own longitudinal drift log and routes the
returned `severity` to their own alerting — mirrors `operational_truth.py`
and `self_mod_cooldown.py`, neither wired into `demo.py`/`runner.py`.
"""
from __future__ import annotations

import math

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


def _dickey_fuller(series: list) -> tuple | None:
    """Pure-stdlib Dickey-Fuller unit-root test (constant, no trend, lag 0).

    Regresses delta y_t on [1, y_{t-1}] via OLS; the t-stat of the y_{t-1}
    coefficient is the DF statistic. Returns (t_stat, p_value, is_stationary),
    or None if the series is degenerate (zero lag-variance / too few points).
    """
    n = len(series) - 1
    if n < 3:
        return None
    x = series[:-1]                                   # y_{t-1}
    dy = [series[i] - series[i - 1] for i in range(1, len(series))]  # delta y_t
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


def compute_consciousness_integrity(drift_values: list[float]) -> dict:
    """ADF stationarity test on a caller-supplied drift-value time series.

    Instead of just checking "is today's drift above threshold?", this asks
    "is the drift TRENDING?" — which catches slow, insidious drift that
    stays below a per-cycle threshold but accumulates over time.

    Args:
        drift_values: the caller's own longitudinal drift readings, oldest
            first. Populate this from whatever drift-detection log your
            deployment already keeps.

    Returns a dict with:
        severity: STABLE / DRIFTING / CRITICAL
        adf_p_value: float (< 0.05 means stationary = good)
        trend_direction: stable / increasing / decreasing
        recommendation: human-readable action
    """
    if len(drift_values) < MIN_INTEGRITY_SAMPLES:
        return {
            "severity": "STABLE",
            "detail": f"Need {MIN_INTEGRITY_SAMPLES - len(drift_values)} more readings for integrity testing",
            "samples": len(drift_values),
        }

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
            severity = "STABLE"
            recommendation = "Drift is stationary (mean-reverting). No action needed."
        elif slope > 0.01:
            severity = "CRITICAL"
            recommendation = (
                "ALERT: Drift is non-stationary and trending upward. "
                "Human review required immediately."
            )
        elif slope > 0:
            severity = "DRIFTING"
            recommendation = (
                "Drift is non-stationary with slight upward trend. "
                "Monitor closely. May indicate gradual alignment shift."
            )
        else:
            severity = "DRIFTING"
            recommendation = "Drift is non-stationary but not trending upward. May stabilize. Continue monitoring."

        return {
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

    except Exception as e:
        return {
            "severity": "STABLE",
            "analysis": "error",
            "detail": f"Integrity check error: {str(e)[:100]}",
        }
