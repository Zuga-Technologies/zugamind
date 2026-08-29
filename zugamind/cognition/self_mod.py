"""Cognition self-modification: the socket SelfModCooldown was written for.

Until 2026-08-29 `gates.self_mod_cooldown.SelfModCooldown` had no caller.
Nothing in the repo ever proposed changing one of the agent's own cognition
files, so there was nothing for the cooldown to cool. This is that path.

Decision 2 (Buga, 2026-08-29): REAL. The agent may APPLY the change, not
only propose it. That makes the reachable set the whole design:

  TARGET   only the per-facet override file `foundation/identity.py`
           already reads at runtime -- DATA_DIR/overrides/<facet>.md --
           addressed by facet NAME. There is no path argument, so the
           shipped persona, Python source, the gates, and this module's own
           audit and cooldown files are not reachable from here.
  GUARD    SelfModCooldown.is_cooling(path) before anything else. A file
           on cooldown is refused and the refusal is journaled with the
           seconds remaining (invariant 5). The class is disk-backed so a
           process restart does not clear the lock; the test constructs a
           second instance on the same db to prove exactly that.
  UNDO     the audit log (ZUGAMIND_COGNITION_MOD_AUDIT, sibling of the
           cooldown db) keeps the BEFORE text of every change, written
           before the file is touched. Full revert to the shipped identity
           is `rm` on the override file: identity.py skips a missing one.
  FLAG     ZUGAMIND_SELF_MOD_ENABLED defaults false (the repo's dark-ship
           rule). Off, a proposal is still recorded in the audit log, still
           journaled, and still starts the cooldown -- nothing is written
           to the override. On, it is applied atomically.

FAIL-CLOSED, unlike share_filter and llm_judge: if the cooldown cannot be
consulted, or the audit record cannot be written, the agent does NOT edit
itself. A brain edit with no lock and no undo record is the one thing this
module exists to prevent. Never raises into the caller. Stdlib only.

What this does NOT include, on purpose: a proposer. Nothing in the loop
composes override text yet -- `zugamind self-mod` is the first caller.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from continuity import journal
from foundation.fs import atomic_write_text
from gates.self_mod_cooldown import SelfModCooldown

logger = logging.getLogger("zugamind.self_mod")

# Mirrors the Facet names in foundation.identity. Addressed by name so the
# reachable set is this tuple and nothing else.
FACETS: tuple[str, ...] = ("sentinel", "deliberative")


def _enabled() -> bool:
    return os.environ.get(
        "ZUGAMIND_SELF_MOD_ENABLED", "false",
    ).strip().lower() not in ("0", "false", "no", "off", "")


def _data_dir() -> Path:
    # At CALL time, not import time: tests redirect foundation.config.DATA_DIR
    # per test, and a module-level constant here would write into the live
    # dogfood deployment (the conftest fixture's own war story).
    from foundation.config import DATA_DIR
    return Path(DATA_DIR)


def override_path(facet: str) -> Path:
    """The file identity.py reads for this facet's runtime override."""
    return _data_dir() / "overrides" / f"{facet}.md"


def audit_path() -> Path:
    """Same rule as self_mod_cooldown._default_db_path -- the two are siblings."""
    default = str(_data_dir() / "cognition_mod_audit.jsonl")
    return Path(os.environ.get("ZUGAMIND_COGNITION_MOD_AUDIT", default))


def propose(facet: str, text: str, *, why: str, evidence: str = "",
            cooldown: Optional[SelfModCooldown] = None,
            now: Optional[float] = None) -> dict[str, Any]:
    """Propose -- and, when enabled, apply -- a new override for `facet`.

    Returns {"applied": bool, "reason": str, "remaining_seconds": float,
             "facet": str, "path": str}. reason is one of:
      applied | disabled | cooling | unknown_facet | empty_text |
      cooldown_error:* | audit_error:* | write_error:* | error:*
    Exactly one journal event is written: cognition_mod_applied,
    cognition_mod_proposed (flag off) or cognition_mod_refused.
    """
    ts = time.time() if now is None else float(now)
    facet = (facet or "").strip()
    try:
        if facet not in FACETS:
            return _refuse(facet, "unknown_facet", why)
        text = (text or "").strip()
        if not text:
            return _refuse(facet, "empty_text", why)

        path = override_path(facet)
        key = str(path)
        try:
            cool = cooldown if cooldown is not None else SelfModCooldown()
            remaining = float(cool.remaining_seconds(key, now=ts))
        except Exception as exc:  # noqa: BLE001 — fail-closed: no lock, no edit
            return _refuse(facet, f"cooldown_error:{type(exc).__name__}:{exc}"[:160], why)
        if remaining > 0:
            journal.append_event("cognition_mod_refused", {
                "facet": facet, "path": key, "reason": "cooling",
                "remaining_seconds": remaining, "why": (why or "")[:200],
            })
            return {"applied": False, "reason": "cooling", "remaining_seconds": remaining,
                    "facet": facet, "path": key}

        enabled = _enabled()
        before = path.read_text(encoding="utf-8") if path.is_file() else ""
        # The undo record exists BEFORE the file is touched: a crash between
        # the two leaves a recoverable override, never an unrecorded one.
        try:
            _audit({"ts": ts, "facet": facet, "path": key, "why": why,
                    "evidence": evidence, "before": before, "after": text,
                    "applied": enabled, "enabled": enabled})
        except Exception as exc:  # noqa: BLE001 — fail-closed: no undo record, no edit
            return _refuse(facet, f"audit_error:{type(exc).__name__}:{exc}"[:160], why)

        if enabled:
            try:
                atomic_write_text(path, text)
            except Exception as exc:  # noqa: BLE001
                # The audit row above says applied=True; correct the record.
                try:
                    _audit({"ts": ts, "facet": facet, "path": key, "applied": False,
                            "error": f"write_error:{exc}"[:160], "corrects_ts": ts})
                except Exception:  # noqa: BLE001
                    pass
                return _refuse(facet, f"write_error:{type(exc).__name__}:{exc}"[:160], why)

        cool.record(key, now=ts)
        journal.append_event("cognition_mod_applied" if enabled else "cognition_mod_proposed", {
            "facet": facet, "path": key, "why": (why or "")[:200],
            "evidence": (evidence or "")[:200], "enabled": enabled,
            "bytes_before": len(before.encode("utf-8")),
            "bytes_after": len(text.encode("utf-8")),
            "cooldown_seconds": cool.cooldown_seconds, "audit": str(audit_path()),
        })
        return {"applied": enabled, "reason": "applied" if enabled else "disabled",
                "remaining_seconds": 0.0, "facet": facet, "path": key}
    except Exception as exc:  # noqa: BLE001 — never raises; fail-closed
        logger.warning("self_mod: unexpected failure, refusing: %s", exc)
        return _refuse(facet, f"error:{type(exc).__name__}:{exc}"[:160], why)


def _audit(row: dict[str, Any]) -> None:
    p = audit_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def _refuse(facet: str, reason: str, why: str) -> dict[str, Any]:
    journal.append_event("cognition_mod_refused", {
        "facet": facet, "reason": reason, "why": (why or "")[:200],
    })
    return {"applied": False, "reason": reason, "remaining_seconds": 0.0,
            "facet": facet, "path": None}
