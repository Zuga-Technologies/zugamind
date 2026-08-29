"""Consciousness Integrity — slow / longitudinal drift health.

Asks a different question than a per-cycle threshold check: not "is today's
drift above threshold?" but "is the drift TRENDING?" — which catches
insidious slow alignment drift that stays below any single-cycle threshold
but accumulates over time.

Method: Augmented Dickey-Fuller (ADF) stationarity test on a drift-value
time series. ADF p-value < 0.05 means the series is mean-reverting (good).
Higher p-value = non-stationary = drift is not snapping back, possibly
trending.

Three independent questions, because one test cannot answer all of them:
  ADF (stationarity)   — does the series snap back to its mean, or wander?
  Mann-Kendall (trend) — is there a monotone trend that is SIGNIFICANT?
  Page-Hinkley (shift) — did the level jump abruptly at some point?

Severity classification:
  STABLE   — mean-reverting, no significant trend, no detected shift
  DRIFTING — non-stationary, but no significant upward trend or shift
  CRITICAL — a significant upward trend, or a detected upward level shift
  UNKNOWN  — the check itself failed (see `analysis`)

Why Mann-Kendall replaced the old rule: severity used to hinge on a raw OLS
slope compared against a hardcoded 0.01. `drift_values` are caller-supplied on
an unknown scale, so 0.01 was a number with no units — on a 0..1 drift metric it
is a strong trend, on a 0..1000 one it is noise, and the same series scaled by a
constant changed its own severity. Mann-Kendall is nonparametric (no Gaussian or
linearity assumption), scale-invariant by construction (it reads only the SIGN
of every pairwise difference), reports an actual significance level instead of a
magnitude, and is documented as valid down to n=10 with the continuity
correction used below. The OLS slope is still reported as `trend_slope` because
it is the readable "how fast" number; it just no longer decides anything.

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
import os

MIN_INTEGRITY_SAMPLES = 10  # need 10+ drift readings for ADF

# Mann-Kendall significance level for calling a trend real.
TREND_ALPHA = float(os.environ.get("ZUGAMIND_INTEGRITY_TREND_ALPHA", "0.05"))
# Page-Hinkley parameters, in units of the series' own standard deviation.
PH_DELTA_SD = float(os.environ.get("ZUGAMIND_INTEGRITY_PH_DELTA_SD", "0.25"))
PH_LAMBDA_SD = float(os.environ.get("ZUGAMIND_INTEGRITY_PH_LAMBDA_SD", "3.0"))

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


def _normal_sf(z: float) -> float:
    """Upper-tail probability of the standard normal. math.erfc, no scipy."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def _mann_kendall(series: list) -> tuple | None:
    """Nonparametric monotone-trend test. Returns (s_stat, z, p_two_sided).

    S counts concordant minus discordant pairs; under the no-trend null S is
    approximately normal with the tie-corrected variance below. The +/-1
    continuity correction on S is what keeps the normal approximation honest at
    the sample sizes this module actually sees (n=10-50). None if n < 4.
    """
    n = len(series)
    if n < 4:
        return None
    s_stat = 0
    for i in range(n - 1):
        for j in range(i + 1, n):
            diff = series[j] - series[i]
            s_stat += (diff > 0) - (diff < 0)
    # Tie correction: groups of equal values contribute no ordering information
    # and must not be counted as if they did.
    counts: dict = {}
    for v in series:
        counts[v] = counts.get(v, 0) + 1
    tie_term = sum(t * (t - 1) * (2 * t + 5) for t in counts.values() if t > 1)
    var_s = (n * (n - 1) * (2 * n + 5) - tie_term) / 18.0
    if var_s <= 0:
        return None
    if s_stat > 0:
        z = (s_stat - 1) / math.sqrt(var_s)
    elif s_stat < 0:
        z = (s_stat + 1) / math.sqrt(var_s)
    else:
        z = 0.0
    return s_stat, z, round(2.0 * _normal_sf(abs(z)), 4)


def _page_hinkley(series: list, delta_sd: float = PH_DELTA_SD,
                  lam_sd: float = PH_LAMBDA_SD) -> tuple:
    """Detect an abrupt UPWARD level shift. Returns (detected, index, peak).

    Accumulates how far each reading runs above the running mean, net of a
    tolerance, and alarms when that accumulation exceeds a threshold. Both
    parameters are expressed in units of the SERIES' OWN standard deviation, so
    they carry meaning on any scale -- the mistake the old slope cutoff made.
    Defaults: ignore excursions under a quarter of an SD, alarm once the
    accumulated excess passes 3 SD. Those are conventions chosen to be
    interpretable, not values derived from this deployment's data; tune them
    with ZUGAMIND_INTEGRITY_PH_DELTA_SD / _PH_LAMBDA_SD once there is enough
    history to calibrate against.
    """
    n = len(series)
    if n < 4:
        return False, None, 0.0
    mean_all = sum(series) / n
    var = sum((x - mean_all) ** 2 for x in series) / n
    sd = math.sqrt(var)
    if sd <= 1e-12:
        return False, None, 0.0  # a flat series cannot shift
    delta, lam = delta_sd * sd, lam_sd * sd
    running, cum, floor, peak = 0.0, 0.0, 0.0, 0.0
    for i, x in enumerate(series):
        running += (x - running) / (i + 1)
        cum += x - running - delta
        floor = min(floor, cum)
        peak = max(peak, cum - floor)
        if (cum - floor) > lam:
            return True, i, round(cum - floor, 6)
    return False, None, round(peak, 6)


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
            # Same tag the degenerate branch carries, for the same reason: a
            # caller filtering on severity alone cannot tell "we looked and it
            # is healthy" from "we could not look". Only the degenerate branch
            # was tagged, so half of the can't-compute cases were invisible.
            "analysis": "insufficient",
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

        # Significance-tested trend (scale-invariant), on the same recent
        # window the slope is read from.
        mk = _mann_kendall(recent)
        if mk is None:
            mk_s, mk_z, mk_p = 0, 0.0, 1.0
        else:
            mk_s, mk_z, mk_p = mk
        trend_significant = mk_p < TREND_ALPHA
        if trend_significant and mk_z > 0:
            trend_direction = "increasing"
        elif trend_significant and mk_z < 0:
            trend_direction = "decreasing"
        else:
            trend_direction = "stable"

        # Abrupt level shift — invisible to both of the above, which describe
        # the series as a whole.
        shift, shift_at, shift_peak = _page_hinkley(drift_values)

        # Classify. Either a proven upward trend or a detected upward shift is
        # CRITICAL on its own: a jump that then holds is stationary around its
        # NEW mean, so stationarity alone would have called it healthy.
        if trend_direction == "increasing":
            severity = "CRITICAL"
            recommendation = (
                "ALERT: drift has a statistically significant upward trend "
                f"(Mann-Kendall p={mk_p}). Human review required immediately."
            )
        elif shift:
            severity = "CRITICAL"
            recommendation = (
                f"ALERT: abrupt upward level shift detected at reading {shift_at}. "
                "Human review required immediately."
            )
        elif is_stationary:
            severity = "STABLE"
            recommendation = "Drift is stationary (mean-reverting). No action needed."
        elif trend_direction == "decreasing":
            severity = "DRIFTING"
            recommendation = ("Drift is non-stationary but trending DOWN. "
                              "Improving; continue monitoring.")
        else:
            severity = "DRIFTING"
            recommendation = ("Drift is non-stationary with no significant trend. "
                              "May stabilize. Continue monitoring.")

        return {
            "severity": severity,
            "adf_statistic": round(adf_stat, 4),
            "adf_p_value": round(p_value, 4),
            "is_stationary": is_stationary,
            # Reported for readability ("how fast"), no longer load-bearing.
            "trend_slope": round(slope, 6),
            "trend_direction": trend_direction,
            "mk_statistic": mk_s,
            "mk_p_value": mk_p,
            "trend_significant": trend_significant,
            "shift_detected": shift,
            "shift_at_index": shift_at,
            "shift_magnitude": shift_peak,
            "samples": len(drift_values),
            "recent_mean_drift": round(sum(recent) / len(recent), 4),
            "recommendation": recommendation,
        }

    except Exception as e:
        # NOT "STABLE". This is a monitor whose entire job is raising a hand,
        # and returning the all-clear label when the check crashed is the one
        # failure mode it cannot afford -- a consumer keying off `severity`
        # (which the docstring says is the intended use) could not tell a
        # healthy series from a broken test. The rest of this package fails
        # closed for the same reason (action_gate blocks when its screen
        # raises); this is that rule applied here (audit 2026-08-29).
        return {
            "severity": "UNKNOWN",
            "analysis": "error",
            "detail": f"Integrity check error: {str(e)[:100]}",
            "recommendation": ("The integrity check itself failed — this is not "
                               "an all-clear. Investigate the checker."),
        }
