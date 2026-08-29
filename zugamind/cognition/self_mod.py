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
  ARMING   the flag alone is not enough. A write also needs an unexpired
           arming marker that a HUMAN creates (`zugamind self-mod --arm`),
           default 60 minutes. Decision 2 stands -- the agent applies, not
           merely proposes -- but it applies inside a window a person just
           opened, rather than forever after one env var was set once. A
           standing switch with no expiry is how a deliberate opt-in becomes
           an unattended capability nobody remembers granting; the same
           reasoning already governs this environment's shared-checkout
           hatch, which expires for exactly that reason. Unarmed, the
           behaviour is identical to flag-off: recorded, journaled, cooled,
           nothing written.

FAIL-CLOSED, unlike share_filter and llm_judge: if the cooldown cannot be
consulted, or the audit record cannot be written, the agent does NOT edit
itself. A brain edit with no lock and no undo record is the one thing this
module exists to prevent. Never raises into the caller. Stdlib only.

Callers: `zugamind self-mod` (a human) and cognition/proposer.py (the loop:
a grounded SELF reflection -> one standing line, under
ZUGAMIND_SELF_MOD_PROPOSER_ENABLED). The override this writes reaches a
prompt only under ZUGAMIND_IDENTITY_PROMPT_ENABLED (gates/action_gate.py).
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
from foundation.identity import override_path  # the loader owns the path rule; re-exported
from gates.self_mod_cooldown import SelfModCooldown

logger = logging.getLogger("zugamind.self_mod")

# Mirrors the Facet names in foundation.identity. Addressed by name so the
# reachable set is this tuple and nothing else.
FACETS: tuple[str, ...] = ("sentinel", "deliberative")


def _enabled() -> bool:
    """Is self-modification armed? Defaults OFF, and fails OFF.

    Uses foundation.config.env_flag: an ALLOW-list, so an unrecognised
    value is OFF and logs. This used to be a deny-list of five
    spellings, which meant ZUGAMIND_SELF_MOD_ENABLED=disabled ARMED it
    (measured 2026-08-29) -- along with "none", "n" and "nope".
    """
    from foundation.config import env_flag  # noqa: WPS433 — lazy, like the siblings
    return env_flag("ZUGAMIND_SELF_MOD_ENABLED", default=False)


def _data_dir() -> Path:
    # At CALL time, not import time: tests redirect foundation.config.DATA_DIR
    # per test, and a module-level constant here would write into the live
    # dogfood deployment (the conftest fixture's own war story).
    from foundation.config import DATA_DIR
    return Path(DATA_DIR)


def audit_path() -> Path:
    """Same rule as self_mod_cooldown._default_db_path -- the two are siblings."""
    default = str(_data_dir() / "cognition_mod_audit.jsonl")
    return Path(os.environ.get("ZUGAMIND_COGNITION_MOD_AUDIT", default))


# How long one arming lasts. Short on purpose: this is the window in which
# the agent may rewrite its own system prompt, and a window nobody has to
# renew is just a switch with extra steps.
ARM_WINDOW_SEC = float(os.environ.get("ZUGAMIND_SELF_MOD_ARM_SEC", "3600"))


def arm_path() -> Path:
    """Where the human-arming marker lives.

    Beside the overrides DIRECTORY, not inside it. A marker inside would make
    arming create overrides/ as a side effect, and "the overrides dir does
    not exist" is a thing the refusal tests legitimately assert about a
    proposal that was rejected. The marker is not an override.
    """
    return override_path(FACETS[0]).parent.parent / ".self_mod_armed"


def arm(*, now: Optional[float] = None) -> dict[str, Any]:
    """Open the arming window. A HUMAN action -- `zugamind self-mod --arm`.

    Deliberately not callable from the reflection loop: cognition/proposer.py
    never imports it, and an agent that could arm itself would make the
    window meaningless.
    """
    ts = time.time() if now is None else float(now)
    path = arm_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps({"armed_at": ts}))
    except Exception as exc:  # noqa: BLE001 — fail-closed: no marker, no write
        return {"armed": False, "reason": f"arm_error:{type(exc).__name__}:{exc}"[:160]}
    journal.append_event("cognition_mod_armed",
                         {"path": str(path), "window_sec": ARM_WINDOW_SEC})
    return {"armed": True, "reason": "armed", "seconds": ARM_WINDOW_SEC,
            "path": str(path)}


def disarm() -> dict[str, Any]:
    """Close the window early. Always safe to call."""
    try:
        arm_path().unlink(missing_ok=True)
    except OSError as exc:
        return {"armed": False, "reason": f"disarm_error:{exc}"[:160]}
    journal.append_event("cognition_mod_disarmed", {})
    return {"armed": False, "reason": "disarmed"}


def _armed_seconds_left(now: float) -> float:
    """Seconds left on the human arming window; 0.0 when unarmed or expired.

    Fail-closed on every error path: an unreadable or malformed marker is
    NOT an arming. The whole point is that a person did something recently
    and deliberately, so anything we cannot confirm is a no.
    """
    try:
        path = arm_path()
        if not path.is_file():
            return 0.0
        armed_at = json.loads(path.read_text(encoding="utf-8")).get("armed_at")
        if not isinstance(armed_at, (int, float)) or isinstance(armed_at, bool):
            return 0.0
        left = ARM_WINDOW_SEC - (now - float(armed_at))
        # A marker stamped in the FUTURE is a clock artefact or a forgery,
        # never a legitimate arming.
        if left > ARM_WINDOW_SEC:
            logger.warning("self_mod: arming marker is stamped in the future — "
                           "treating as unarmed")
            return 0.0
        return max(0.0, left)
    except Exception as exc:  # noqa: BLE001
        logger.warning("self_mod: arming marker unreadable (%s) — unarmed", exc)
        return 0.0


def propose(facet: str, text: str, *, why: str, evidence: str = "",
            cooldown: Optional[SelfModCooldown] = None,
            now: Optional[float] = None) -> dict[str, Any]:
    """Propose -- and, when enabled, apply -- a new override for `facet`.

    Returns {"applied": bool, "reason": str, "remaining_seconds": float,
             "facet": str, "path": str}. reason is one of:
      applied | disabled | not_armed | cooling | unknown_facet | empty_text |
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
            # try_claim, not remaining_seconds. The old pair was a
            # check-then-act with the audit write AND the file write inside
            # the window, and that window is the only rate limit on an agent
            # rewriting its own system prompt. Measured 2026-08-29: eight
            # threads against one 24h lock applied up to 8 of 8, and six
            # separate processes breached it in five runs of twelve. The claim
            # is one conditional UPSERT -- see gates/self_mod_cooldown.try_claim.
            remaining = float(cool.try_claim(key, now=ts))
        except Exception as exc:  # noqa: BLE001 — fail-closed: no lock, no edit
            return _refuse(facet, f"cooldown_error:{type(exc).__name__}:{exc}"[:160], why)
        if remaining > 0:
            journal.append_event("cognition_mod_refused", {
                "facet": facet, "path": key, "reason": "cooling",
                "remaining_seconds": remaining, "why": (why or "")[:200],
            })
            return {"applied": False, "reason": "cooling", "remaining_seconds": remaining,
                    "facet": facet, "path": key}

        # Two conditions, not one: the standing switch AND a live human
        # arming window. See ARMING in the module docstring.
        armed_for = _armed_seconds_left(ts)
        enabled = _enabled() and armed_for > 0
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
        journal.append_event("cognition_mod_applied" if enabled else "cognition_mod_proposed", {
            "facet": facet, "path": key, "why": (why or "")[:200],
            "evidence": (evidence or "")[:200], "enabled": enabled,
            "bytes_before": len(before.encode("utf-8")),
            "bytes_after": len(text.encode("utf-8")),
            "cooldown_seconds": cool.cooldown_seconds, "audit": str(audit_path()),
        })
        # Distinguish the two ways a write can be withheld: the standing
        # switch is off, or nobody has armed a window. They call for
        # different actions from a human, so they must not share a reason.
        if enabled:
            reason = "applied"
        elif not _enabled():
            reason = "disabled"
        else:
            reason = "not_armed"
        return {"applied": enabled, "reason": reason,
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
