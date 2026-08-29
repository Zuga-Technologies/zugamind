"""Draft-post surface: the socket llm_judge was written for.

Until 2026-08-29 `gates.llm_judge.judge_post` had no caller. Nothing the
agent composed about its own work ever travelled towards a human, so there
was no draft for it to judge. This is that surface, and it is deliberately
NOT a social-media integration: a "post" here is a REPORT -- prose the agent
writes about what it did, destined for a human (`zugamind report`).

Two narrative gates sit in front of the emit, in this order:

  1. work_claim.check_work_claim   free, deterministic. "I fixed X" with no
                                   commit touching X in the window is refused
                                   here and the judge is never called.
  2. llm_judge.judge_post          local model, bounded timeout, FAIL-OPEN.
                                   Catches what the verb heuristic cannot
                                   ("ClickHouse is now in our stack").

Both are fail-open by their own contracts and this module preserves that: a
judge that is absent (no model pulled -- BugaPC today), times out, or
crashes yields ALLOW and the journal says so. A gate that drops something
silently is indistinguishable from a broken gate, so every refusal is a
`report_suppressed` event carrying the stage and reason (invariant 5).

Delivery is the journal only (decision 1): `report_emitted`. The CLI prints
an emitted report because a human asked for it; nothing here posts anywhere.
Stdlib only. Never raises into the caller.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from continuity import journal
from gates.llm_judge import judge_post
from gates.work_claim import check_entity_grounding, check_work_claim

logger = logging.getLogger("zugamind.reports")

# Characters of a harness reply quoted into the report. The reply is where a
# fabricated accomplishment enters the account, so it is QUOTED, not
# paraphrased -- the gates must see the claim in the text they judge.
_REPLY_EXCERPT = 240


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def compose_report(window_minutes: int = 60, limit: int = 500) -> str:
    """Plain prose about the last `window_minutes`, built deterministically
    from the journal. Returns "" when nothing happened -- no report beats
    an empty one."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).isoformat()
    events = journal.read_events(since_iso=since, limit=limit)
    cycles = [e for e in events if e.get("kind") == "cycle"]
    acted = [e for e in cycles if e.get("winner")]
    wakes = [e for e in events
             if e.get("kind") == "harness_invocation" and e.get("ok") and not e.get("dry_run")]
    alarms = [e for e in events if e.get("kind") == "alarm"]
    if not (cycles or wakes or alarms):
        return ""

    lines = [f"In the last {window_minutes} minutes: {_plural(len(cycles), 'cycle')}, "
             f"{len(acted)} acted."]
    modules = sorted({str((e.get("winner") or {}).get("source_module") or "?") for e in acted})
    if modules:
        lines.append(f"Acted on: {', '.join(modules)}.")
    for w in wakes:
        reply = " ".join(str(w.get("stdout") or "").split())[:_REPLY_EXCERPT]
        lines.append(f"Harness {w.get('harness', '?')} replied: {reply}" if reply
                     else f"Harness {w.get('harness', '?')} ran and said nothing.")
    if alarms:
        lines.append(f"{_plural(len(alarms), 'alarm')}: "
                     + "; ".join(str(a.get("detail") or "")[:100] for a in alarms) + ".")
    return "\n".join(lines)


def emit_report(text: str, *, commits: Optional[List[str]] = None,
                repo_root: Optional[str] = None, window_minutes: int = 30) -> dict[str, Any]:
    """Run a draft through work_claim, then llm_judge, and journal the outcome.

    Returns {"emitted": bool, "stage": "work_claim"|"judge"|None,
             "reason": str, "unbacked": [..], "judge": str|None}.
    Exactly one journal event is written: `report_emitted` or
    `report_suppressed`. Never raises.
    """
    text = (text or "").strip()
    try:
        wc = check_work_claim(text, window_minutes=window_minutes,
                              commits=commits, repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001 — fail-open by contract
        logger.warning("reports: check_work_claim raised, failing open: %s", exc)
        wc = {"backed": True, "unbacked": [], "reason": f"guard_error:{exc}"[:160]}
    if not isinstance(wc, dict):
        # A malformed RETURN is as likely as a raise and used to escape the
        # try above -- wc.get(...) then raised AttributeError out of the CLI,
        # writing zero journal events and breaking two documented guarantees
        # ("exactly one journal event", "never raises"). cognition/thoughts.py
        # gets this right; this did not (audit 2026-08-29).
        wc = {"backed": True, "unbacked": [],
              "reason": f"gate_malformed:{type(wc).__name__}"}
    if not wc.get("backed", True):
        return _suppress(text, "work_claim", str(wc.get("reason")), wc.get("unbacked") or [])

    # The NOUN half of the same question. check_work_claim matches claim VERBS
    # against commits; a claim with no listed verb ("ClickHouse is now in our
    # stack" -- the example this module's own docstring names) walks straight
    # past it. check_entity_grounding is the half that catches it: free,
    # deterministic, stdlib, and already running on harness replies in
    # stream/runner.py -- but only ADVISORY there, while this path, the one
    # that reaches a human, ran the weaker composition. It goes after
    # work_claim and before the judge deliberately: it is a SUPPRESS-action
    # gate whose dictionary is empty on Windows (see work_claim's own
    # docstring), so a false positive here costs one refused report rather
    # than a silenced agent.
    try:
        eg = check_entity_grounding(text, repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001 — fail-open, same contract as above
        logger.warning("reports: check_entity_grounding raised, failing open: %s", exc)
        eg = {"grounded": True, "ungrounded": [], "reason": f"guard_error:{exc}"[:160]}
    if not isinstance(eg, dict):
        eg = {"grounded": True, "ungrounded": [],
              "reason": f"gate_malformed:{type(eg).__name__}"}
    if not eg.get("grounded", True):
        return _suppress(text, "entity_grounding", str(eg.get("reason")),
                         eg.get("ungrounded") or [])

    try:
        jv = judge_post(text, commits=commits, window_minutes=window_minutes)
    except Exception as exc:  # noqa: BLE001 — judge_post is itself fail-open; belt and braces
        logger.warning("reports: judge_post raised, failing open: %s", exc)
        jv = {"verdict": "ALLOW", "reason": f"guard_error:{exc}"[:160]}
    if not isinstance(jv, dict):
        jv = {"verdict": "ALLOW", "reason": f"gate_malformed:{type(jv).__name__}"}
    if str(jv.get("verdict", "")).strip().upper() == "SUPPRESS":
        return _suppress(text, "judge", str(jv.get("reason")), [])

    # Whether anything actually CHECKED this. Both narrative gates are
    # fail-open by contract, so with no git and no local model a report sails
    # through and reads exactly like one both gates passed. The reasons were
    # journaled but never surfaced, so the one reader who could act on it --
    # the human holding the draft -- could not tell (audit 2026-08-29).
    unchecked = [
        name for name, reason in (
            ("work_claim", str(wc.get("reason", ""))),
            ("entity_grounding", str(eg.get("reason", ""))),
            ("judge", str(jv.get("reason", ""))),
        )
        if reason.startswith(("probe_unavailable", "gate_error", "guard_error",
                              "gate_malformed", "judge_unavailable", "judge_error",
                              "no_repo"))
    ]
    journal.append_event("report_emitted", {
        "text": text, "work_claim": wc.get("reason"),
        "entity_grounding": eg.get("reason"), "judge": jv.get("reason"),
        "unchecked_by": unchecked,
    })
    return {"emitted": True, "stage": None, "reason": "emitted",
            "unbacked": [], "judge": jv.get("reason"),
            "gated": not unchecked, "unchecked_by": unchecked}


def _suppress(text: str, stage: str, reason: str, unbacked: list) -> dict[str, Any]:
    journal.append_event("report_suppressed", {
        "text": text, "stage": stage, "reason": reason, "unbacked": list(unbacked)[:3],
    })
    return {"emitted": False, "stage": stage, "reason": reason,
            "unbacked": list(unbacked)[:3], "judge": None}
