"""Self-modification proposer: the part of the lane that was still missing.

After 2026-08-29 the agent COULD rewrite its own runtime override
(cognition/self_mod.py) but nothing in the loop ever composed a change --
the CLI was the only caller. This is the smallest honest composer:

  a SELF-domain reflection that a real source answered  (the only kind
  of pair that is about the agent itself AND grounded)
    -> shown to the local model: "NONE, or ONE standing line"
    -> that line is appended to the SENTINEL's override via
       self_mod.propose(), so the 24h cooldown, the audit log with the
       BEFORE text, and the apply flag are all inherited, not re-done.

Why the sentinel and not the deliberative self: the local model is the one
reflecting, and its own note is the smallest blast radius. The
deliberative override stays human-curated.

Bounds: one line, MAX_LINE_CHARS; the override stops growing at
MAX_OVERRIDE_CHARS; an identical line is never re-proposed; the cooldown
allows one proposal per facet per 24h. Every non-proposal after candidacy
is a journaled `self_mod_proposal_skipped` with its reason (invariant 5).

Ships DARK behind ZUGAMIND_SELF_MOD_PROPOSER_ENABLED: off, no model call
is made and the skip is journaled. Never raises. Stdlib only.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from cognition import self_mod
from cognition.models.ollama import ollama_query
from continuity import journal
from foundation import identity
from gates.self_mod_cooldown import SelfModCooldown

logger = logging.getLogger("zugamind.proposer")

FACET = "sentinel"
MAX_OVERRIDE_CHARS = 2000
MAX_LINE_CHARS = 200
_TIMEOUT_S = int(os.environ.get("ZUGAMIND_PROPOSER_TIMEOUT_S", "20"))

_SYSTEM = (
    "You are reviewing one Socratic reflection an autonomous agent made about "
    "ITSELF: a question it asked about its own behaviour, and the answer a real "
    "source (its own code or journal) gave. Decide whether this yields ONE "
    "durable, standing note about how the agent should think or act from now "
    "on -- a lesson about itself, not a fact about the world.\n"
    "Rules:\n"
    "- If there is no such lesson, answer exactly: NONE\n"
    "- Otherwise answer exactly ONE line, imperative mood, under 200 characters, "
    "no preamble, no quotes, no bullet."
)


def _enabled() -> bool:
    """Is self-modification armed? Defaults OFF, and fails OFF.

    Uses foundation.config.env_flag: an ALLOW-list, so an unrecognised
    value is OFF and logs. This used to be a deny-list of five
    spellings, which meant ZUGAMIND_SELF_MOD_PROPOSER_ENABLED=disabled ARMED it
    (measured 2026-08-29) -- along with "none", "n" and "nope".
    """
    from foundation.config import env_flag  # noqa: WPS433 — lazy, like the siblings
    return env_flag("ZUGAMIND_SELF_MOD_PROPOSER_ENABLED", default=False)


def _prompt(question: str, answer: str) -> str:
    return (f"QUESTION the agent asked about itself:\n{question[:600]}\n\n"
            f"ANSWER a real source gave:\n{answer[:900]}\n\n"
            "Standing note (or NONE):")


def _one_line(reply: str) -> str:
    """First non-empty line of the model's reply, cleaned; '' for NONE."""
    for raw in (reply or "").splitlines():
        line = raw.strip().strip("-*• ").strip().strip('"“”').strip()
        if not line:
            continue
        if line.upper().startswith("NONE"):
            return ""
        return line[:MAX_LINE_CHARS].rstrip()
    return ""


def propose_from_reflection(result: dict[str, Any], *,
                            cooldown: Optional[SelfModCooldown] = None) -> dict[str, Any]:
    """Turn one reflection result (engine.reflect_once's dict) into a proposal.

    Returns {"proposed": bool, "reason": str, "line": str|None, "verdict": dict|None}.
    reason: not_a_candidate | disabled | override_full | model_unavailable |
            none | duplicate | recorded | applied | <self_mod refusal> | error:*
    """
    try:
        question = str(result.get("question") or "").strip()
        if (str(result.get("domain") or "") != "SELF" or not result.get("answered")
                or not question):
            return {"proposed": False, "reason": "not_a_candidate", "line": None, "verdict": None}
        if not _enabled():
            return _skip("disabled", question)

        path = identity.override_path(FACET)
        current = path.read_text(encoding="utf-8") if path.is_file() else ""
        if len(current) >= MAX_OVERRIDE_CHARS:
            return _skip("override_full", question)

        reply = ollama_query(_prompt(question, str(result.get("answer") or "")),
                             max_tokens=80, system=_SYSTEM, timeout=_TIMEOUT_S)
        if reply is None:
            return _skip("model_unavailable", question)
        line = _one_line(reply)
        if not line:
            return _skip("none", question)
        if line in [l.strip() for l in current.splitlines()]:
            return _skip("duplicate", question)

        new_text = f"{current.rstrip()}\n{line}" if current.strip() else line
        # ACTOR_AGENT (the default, stated explicitly): this is the
        # autonomous loop, so both the cooldown and the arming window bind it.
        verdict = self_mod.propose(
            FACET, new_text, why=f"reflection: {question[:120]}",
            evidence=str(result.get("answer") or "")[:300], cooldown=cooldown,
        )
        reason = {"applied": "applied", "disabled": "recorded"}.get(
            str(verdict.get("reason")), str(verdict.get("reason")))
        return {"proposed": reason in ("applied", "recorded"), "reason": reason,
                "line": line, "verdict": verdict}
    except Exception as exc:  # noqa: BLE001 — idle-cycle work never raises
        logger.warning("proposer: failed, no proposal: %s", exc)
        return {"proposed": False, "reason": f"error:{type(exc).__name__}:{exc}"[:160],
                "line": None, "verdict": None}


def _skip(reason: str, question: str) -> dict[str, Any]:
    journal.append_event("self_mod_proposal_skipped", {
        "facet": FACET, "reason": reason, "question": question[:200],
    })
    return {"proposed": False, "reason": reason, "line": None, "verdict": None}
