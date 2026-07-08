"""Value gate — post-hoc usefulness scoring + bid feedback.

The agent has many UPFRONT throttles (habituation, per-scanner caps, control
prior, focus-one-at-a-time, goal_block parking, budget caps) but no signal on
whether finished work MATTERED. So wasteful/useless task types keep winning bids
forever — nothing starves them. This adds the missing reward loop:

  1. score_action(...)  — after a cycle's action resolves, judge "did this
     change real state?" -> 1, else 0. Deterministic fast-paths first (landed
     code == 1; pure think/none == 0); the local model only judges the
     ambiguous middle (e.g. alerts). Local-only, fail-open (no score = neutral).
  2. persisted to a value_scores table (rolling), keyed by
     (source_module, trigger_type) — the dimensions known BEFORE competition.
  3. _apply_value_prior(bids) — re-weights bids by their type's rolling
     value-rate BEFORE competition, mirroring _apply_control_prior: low-value
     types are dampened to a FLOOR (never silenced), high-value get a small
     boost. So budget flows to work that has paid off, volume self-limits, and
     "landed real change" becomes the gradient.

Ships DARK: ZUGAMIND_VALUE_GATE_ENABLED defaults false — _apply_value_prior is a
no-op and bids are byte-identical when off (telemetry still emits so the scorer
can be verified dark). Stdlib + sqlite3 only; every entry point best-effort and
never raises into the cycle. Composes with control prior (control = "can I act
on it", value = "did acting pay off").
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Optional

logger = logging.getLogger("zugamind.value_gate")

# Rolling window of recent scores per (module, trigger_type) used for the rate.
_WINDOW = int(os.environ.get("ZUGAMIND_VALUE_WINDOW", "50"))
# Below this rate a type is dampened; the multiplier is the rate itself,
# floored so a chronically-useless type is quieted, never fully silenced.
_DAMPEN_BELOW = float(os.environ.get("ZUGAMIND_VALUE_DAMPEN_BELOW", "0.3"))
_VALUE_FLOOR = float(os.environ.get("ZUGAMIND_VALUE_FLOOR", "0.1"))
_VALUE_BOOST = float(os.environ.get("ZUGAMIND_VALUE_BOOST", "1.1"))
_BOOST_ABOVE = float(os.environ.get("ZUGAMIND_VALUE_BOOST_ABOVE", "0.7"))
# Need at least this many samples before a rate is trusted to re-weight.
_MIN_SAMPLES = int(os.environ.get("ZUGAMIND_VALUE_MIN_SAMPLES", "5"))

# Action classes. deliverable + research sets are single-sourced from the
# contract's canonical definitions (decision_contract.py) so the call sites
# (here, and decision_contract.py) can't drift.
# deliverable: real-state changes -> value comes from the recorded outcome
#   (e.g. a plan/task status) keyed by corr_id; DEFER until it lands.
# research: spends, but soft check (artifact = provisional credit). Its own class.
# cognition: think-only; honestly 0 but the prior floor (_VALUE_FLOOR) keeps it
#   reachable -- never auto-zeroed in re-weighting. `remediate` lives HERE:
#   it is a reflect/analyze thought, not a real-state change.
from foundation.contracts.decision_contract import (
    DELIVERABLE_ACTIONS as _DELIVERABLE_ACTIONS,
    RESEARCH_ACTIONS as _RESEARCH_ACTIONS,
)
_COGNITION_ACTIONS = {"none", "reflect", "analyze", "log", "observe", "remediate"}
# 'alert' (and any unlisted action) stays ambiguous -> local-model fallback.


def _enabled() -> bool:
    return os.environ.get(
        "ZUGAMIND_VALUE_GATE_ENABLED", "false",
    ).strip().lower() not in ("0", "false", "no", "off", "")


def _db_path() -> str:
    try:
        from foundation.config import DATA_DIR
        return str(DATA_DIR / "value_scores.db")
    except Exception:
        root = os.environ.get("ZUGAMIND_REPO_ROOT", os.getcwd())
        return os.path.join(root, "data", "zugamind_value_scores.db")


def _ensure_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS value_scores ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT DEFAULT CURRENT_TIMESTAMP, "
        "source_module TEXT, trigger_type TEXT, action TEXT, value INTEGER, reason TEXT, "
        "corr_id TEXT, status TEXT DEFAULT 'final')"
    )
    # Additive migration for tables created before corr_id/status existed.
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(value_scores)").fetchall()}
        if "corr_id" not in cols:
            conn.execute("ALTER TABLE value_scores ADD COLUMN corr_id TEXT")
        if "status" not in cols:
            conn.execute("ALTER TABLE value_scores ADD COLUMN status TEXT DEFAULT 'final'")
    except Exception as exc:
        logger.debug("value_scores migration skipped: %s", exc)


def _recorded_outcome(corr_id: str, db_path: Optional[str] = None) -> Optional[int]:
    """Most recent FINAL value recorded for this corr_id, or None if not yet known."""
    if not corr_id:
        return None
    try:
        with sqlite3.connect(_db_path() if db_path is None else db_path, timeout=2.0) as conn:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT value FROM value_scores WHERE corr_id=? AND status='final' "
                "AND value IS NOT NULL ORDER BY id DESC LIMIT 1", (corr_id,),
            ).fetchone()
    except Exception as exc:
        logger.debug("_recorded_outcome read failed: %s", exc)
        return None
    return None if row is None else float(row[0])


def _emit(event: str, payload: dict, cycle_id: Optional[int] = None) -> None:
    """Structured telemetry for the value gate. Log-only in the OSS core --
    wire this to your own event bus/stream if you want it persisted/queryable."""
    try:
        logger.info(
            "value_gate.%s cycle_id=%s payload=%s",
            event, cycle_id, json.dumps(payload, default=str),
        )
    except Exception as exc:
        logger.debug("value_gate emit failed: %s", exc)


def _judge_value(
    action: str, trigger_type: str, summary: str,
    corr_id: Optional[str] = None, db_path: Optional[str] = None,
) -> tuple[Optional[int], str]:
    """Layered value judgement. Returns (value, reason) where value is 0|1, or
    None when DEFERRED (a deliverable whose recorded outcome isn't in yet).

    deliverable -> read the recorded outcome by corr_id; defer if none.
    research -> soft provisional credit (artifact present). cognition -> 0,
    but the prior floor keeps it reachable. alert/unknown -> local-model fallback."""
    a = (action or "").lower()
    if a in _DELIVERABLE_ACTIONS:
        outcome = _recorded_outcome(corr_id or "", db_path=db_path)
        if outcome is not None:
            return outcome, "deliverable: recorded performance outcome"
        return None, "deliverable: deferred -- awaiting performance outcome"
    if a in _RESEARCH_ACTIONS:
        # Spends, but soft check: artifact produced = provisional credit, never auto-zero.
        if (summary or "").strip():
            return 1, "research: provisional credit (artifact present)"
        return 0, "research: no artifact yet (floored by prior)"
    if a in _COGNITION_ACTIONS:
        return 0, "cognition: think-only (floored by prior, never silenced)"
    # Ambiguous (e.g. 'alert'): valuable only if it flagged something real.
    try:
        from cognition.models.ollama import ollama_available, ollama_query
        if ollama_available():
            q = (
                "Did this autonomous action change real state or surface a REAL, "
                "actionable problem (answer 1), or was it narration / re-noting a "
                f"known or non-existent issue (answer 0)?\nAction: {action}\n"
                f"Trigger: {trigger_type}\nSummary: {summary[:300]}\n"
                "Answer with ONLY the digit 0 or 1."
            )
            r = (ollama_query(q, max_tokens=3) or "").strip()
            if r.startswith("1"):
                return 1, "model: real/actionable"
            if r.startswith("0"):
                return 0, "model: narration/known"
    except Exception:
        pass
    return 0, "ambiguous, default low"


def record_outcome(
    corr_id: str, value: int, db_path: Optional[str] = None,
    source_module: str = "", trigger_type: str = "", action: str = "",
    reason: str = "reconcile: authoritative outcome", cycle_id: Optional[int] = None,
) -> Optional[int]:
    """Authoritative value write at reconcile, keyed by corr_id (spec step 2).

    The v1 truth signal is a recorded plan/task outcome (completed=1 /
    failed=0). If a pending provisional row exists for this corr_id, promote
    IT to final (reusing its stored source_module/trigger_type so the score
    lands on the same auction key the value-prior reads). Otherwise insert a
    fresh final row from the passed dimensions. Idempotent: a corr_id already
    final is left untouched. No-op when disabled. Best-effort -- never raises
    into the cycle."""
    v = float(value)
    _emit("value_outcome_recorded",
          {"corr_id": corr_id, "value": v, "source_module": source_module,
           "trigger_type": trigger_type, "action": action, "enabled": _enabled()},
          cycle_id=cycle_id)
    if not _enabled() or not corr_id:
        return None
    try:
        with sqlite3.connect(_db_path() if db_path is None else db_path, timeout=5.0) as conn:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT id, status FROM value_scores WHERE corr_id=? "
                "ORDER BY id DESC LIMIT 1", (corr_id,),
            ).fetchone()
            if row and row[1] == "final":
                return v  # already authoritative -- idempotent
            if row:
                conn.execute(
                    "UPDATE value_scores SET value=?, status='final', reason=? WHERE id=?",
                    (v, reason, row[0]),
                )
            else:
                conn.execute(
                    "INSERT INTO value_scores (source_module, trigger_type, action, value, "
                    "reason, corr_id, status) VALUES (?, ?, ?, ?, ?, ?, 'final')",
                    (source_module, trigger_type, action, v, reason, corr_id),
                )
            conn.commit()
    except Exception as exc:
        logger.debug("record_outcome persist failed: %s", exc)
    return v


def score_action(
    source_module: str, action: str, trigger_type: str = "",
    summary: str = "", cycle_id: Optional[int] = None, db_path: Optional[str] = None,
    corr_id: Optional[str] = None, status: str = "final",
) -> Optional[int]:
    """Score the just-resolved action and persist it. Returns 0|1, None when
    disabled/error, or None for a DEFERRED deliverable (written status='pending',
    value NULL -- the authoritative score lands later at reconcile by corr_id).
    `status` is the caller's intent ('final' at reconcile, 'pending' for the
    dispatch-time provisional/telemetry write). Telemetry emits regardless of flag."""
    value, reason = _judge_value(action, trigger_type, summary, corr_id=corr_id, db_path=db_path)
    deferred = value is None
    row_status = "pending" if (deferred or status == "pending") else "final"
    _emit("value_scored",
          {"source_module": source_module, "action": action,
           "trigger_type": trigger_type, "value": value, "reason": reason,
           "corr_id": corr_id or "", "status": row_status, "deferred": deferred,
           "enabled": _enabled()},
          cycle_id=cycle_id)
    if not _enabled():
        return None
    try:
        with sqlite3.connect(_db_path() if db_path is None else db_path, timeout=5.0) as conn:
            _ensure_table(conn)
            conn.execute(
                "INSERT INTO value_scores (source_module, trigger_type, action, value, reason, "
                "corr_id, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (source_module, trigger_type, action,
                 None if deferred else int(value), reason, corr_id or "", row_status),
            )
            conn.commit()
    except Exception as exc:
        logger.debug("score_action persist failed: %s", exc)
    return value


def value_rate(
    source_module: str, trigger_type: str = "", db_path: Optional[str] = None,
) -> Optional[tuple[float, int]]:
    """Rolling (rate, sample_count) over the last _WINDOW scores for this key,
    or None if disabled / unavailable. trigger_type='' aggregates the module."""
    if not _enabled():
        return None
    try:
        with sqlite3.connect(_db_path() if db_path is None else db_path, timeout=2.0) as conn:
            _ensure_table(conn)
            if trigger_type:
                rows = conn.execute(
                    "SELECT value FROM value_scores WHERE source_module=? AND trigger_type=? "
                    "AND value IS NOT NULL AND status='final' "
                    "ORDER BY id DESC LIMIT ?", (source_module, trigger_type, _WINDOW),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT value FROM value_scores WHERE source_module=? "
                    "AND value IS NOT NULL AND status='final' "
                    "ORDER BY id DESC LIMIT ?", (source_module, _WINDOW),
                ).fetchall()
    except Exception as exc:
        logger.debug("value_rate read failed: %s", exc)
        return None
    if not rows:
        return None
    vals = [r[0] for r in rows]
    return sum(vals) / len(vals), len(vals)


def _apply_value_prior(bids: list, db_path: Optional[str] = None) -> tuple[list, Optional[list]]:
    """Re-weight bids by their type's rolling value-rate. No-op + None snapshot
    when disabled. Mirrors _apply_control_prior (in-place, floored, telemetry).
    Only re-weights a key once it has >= _MIN_SAMPLES history."""
    if not _enabled():
        return bids, None
    snapshot = []
    for bid in bids:
        module = getattr(bid, "source_module", "") or ""
        ctx = bid.context if isinstance(getattr(bid, "context", None), dict) else {}
        ttype = ""
        trigs = ctx.get("triggers")
        if isinstance(trigs, list) and trigs and isinstance(trigs[0], dict):
            ttype = trigs[0].get("type", "") or ""
        rr = value_rate(module, ttype, db_path=db_path)
        if rr is None:
            continue
        rate, n = rr
        if n < _MIN_SAMPLES:
            continue
        s0 = bid.salience
        if rate < _DAMPEN_BELOW:
            bid.salience = max(_VALUE_FLOOR, bid.salience * max(rate, _VALUE_FLOOR))
        elif rate >= _BOOST_ABOVE:
            bid.salience = min(1.0, bid.salience * _VALUE_BOOST)
        if abs(bid.salience - s0) > 1e-9:
            snapshot.append({"m": module, "t": ttype, "rate": round(rate, 3),
                             "n": n, "from": round(s0, 4), "to": round(bid.salience, 4)})
    return bids, snapshot


__all__ = ["score_action", "record_outcome", "value_rate", "_apply_value_prior"]
