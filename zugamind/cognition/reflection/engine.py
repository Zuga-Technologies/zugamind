"""What the mind does with an idle cycle.

Before this, the tenth consecutive idle cycle called
`transition_state(state, "REFLECTING", ...)` and stopped. REFLECTING was a
mood label: a word in state.json, no thought behind it. This is the thought.

Two things run on that cadence, both longitudinal, both local-model or
cheaper, neither able to spend a paid token:

  reflect_once()    pick the last winner that actually mattered, ask one
                    grounded Socratic question about it, resolve that
                    question against a real source, journal the pair.
  drift_integrity() read the drift series the floor calibrator has been
                    journaling all along and ask whether it is TRENDING --
                    a question no per-cycle threshold can answer.

Both are best-effort and never raise into the cycle: an idle cycle that
cannot reflect is still a valid idle cycle.

A completed reflection is also a CANDIDATE THOUGHT: after it is journaled it
is handed to cognition.thoughts, where gates.share_filter decides whether it
is worth a human's attention (journal-only delivery; see that module).

Kill switch: ZUGAMIND_REFLECTION_ENABLED=false restores the old behaviour
(state transition only, no model calls).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from continuity import journal
from gates.integrity import MIN_INTEGRITY_SAMPLES, compute_consciousness_integrity
from gates.operational_truth import format_block, is_stale_operational

from ..thoughts import consider_thought
from .answer_router import answer_question
from .domain_classifier import classify_domain
from .question_generator import generate_question

logger = logging.getLogger("zugamind.reflection")

# share_filter needs a confidence, and a reflection has no number of its own.
# This is NOT an invented probability: it is a two-step ladder on the one
# thing observable about the pair -- whether a real source was consulted for
# the answer. A question nothing resolved is a guess, and lands under
# share_filter.CONFIDENCE_FLOOR (0.6) on purpose; one a source answered
# clears it. Anything finer would be a number this engine cannot justify.
_CONFIDENCE_ANSWERED = 0.7
_CONFIDENCE_UNANSWERED = 0.2

# How far back to look for something worth reflecting on. An idle stretch of
# ten cycles is minutes to hours depending on POLL_INTERVAL, so the last real
# winner can be a good way back in the journal.
_WINNER_LOOKBACK = int(os.environ.get("ZUGAMIND_REFLECT_LOOKBACK", "200"))
# The drift series is journaled only when the floor MOVES, so a wide window
# holds few rows -- this is a longitudinal read, not a per-cycle one.
_DRIFT_LOOKBACK = int(os.environ.get("ZUGAMIND_DRIFT_LOOKBACK", "1000"))


def _enabled() -> bool:
    return os.environ.get(
        "ZUGAMIND_REFLECTION_ENABLED", "true",
    ).strip().lower() not in ("0", "false", "no", "off")


def _recent_winner(limit: int = _WINNER_LOOKBACK) -> Optional[dict]:
    """The most recent cycle winner, or None if the mind has only been idle.

    An idle cycle has nothing of its own to think about, so the subject is the
    last thing that DID clear the workspace -- which is also the thing most
    worth a second look, since it was acted on.
    """
    try:
        events = journal.read_events(limit=limit)
    except Exception as exc:  # noqa: BLE001 — a journal read must never break a cycle
        logger.debug("reflection: journal read failed: %s", exc)
        return None
    for event in reversed(events):
        if event.get("kind") != "cycle":
            continue
        winner = event.get("winner")
        if isinstance(winner, dict) and winner:
            return winner
    return None


def _as_thought(result: dict) -> dict:
    """The reflection in the shape share_filter reads.

    topic_class is always "question" -- that is what a Socratic pass
    produces -- and ask_text is the question itself, so the guard's
    question-form rule is what judges it: a "question" that does not end in
    "?" is not one, and the guard says so.
    """
    text = result["question"]
    if result.get("answer"):
        text = f"{text}\n{result['answer']}"
    return {
        "text": text,
        "confidence": result["confidence"],
        "topic_class": "question",
        "proposed_action": "",
        "ask_text": result["question"],
    }


def reflect_once() -> Optional[dict]:
    """One Socratic pass over the last real winner. Journals; never raises."""
    if not _enabled():
        return None
    try:
        trigger = _recent_winner()
        if not trigger:
            journal.append_event("reflection_skipped", {"reason": "no_subject"})
            return None

        # Freshness before thought. The subject came out of the journal, so it
        # is by definition a memory -- and a memory of an operational fact is
        # exactly what this agent confabulates into the present tense ("still
        # down", "hasn't improved"). If the live probe contradicts it, the
        # honest move is not to reason on top of it.
        subject_text = str(trigger.get("content") or "")
        if is_stale_operational(subject_text):
            journal.append_event("reflection_skipped", {
                "reason": "stale_subject",
                "subject": subject_text[:200],
            })
            return None

        domain = "SELF"
        try:
            domain = str(classify_domain(trigger).get("domain") or "SELF")
        except Exception as exc:  # noqa: BLE001 — a bad classification is not a dead cycle
            logger.debug("reflection: classify failed (%s); defaulting to SELF", exc)

        # The same VERIFIED LIVE STATE block the freshness gate was written to
        # produce, handed to the question generator so a question cannot be
        # built on a service the runtime does not actually have. Empty string
        # when the deployer configured no services -- no block beats a
        # misleading one.
        question = generate_question(trigger, domain, grounding=format_block())
        if not question:
            journal.append_event("reflection_skipped", {
                "reason": "no_question", "domain": domain})
            return None

        answer = answer_question(
            question["text"], question.get("answer_source_hint", "none"),
            trigger=trigger,
        )

        answered = bool(answer.get("success"))
        result = {
            "domain": domain,
            "question": question["text"],
            "source": answer.get("source"),
            "answered": answered,
            "answer": str(answer.get("content") or "")[:600],
            "latency_ms": answer.get("latency_ms"),
            "confidence": _CONFIDENCE_ANSWERED if answered else _CONFIDENCE_UNANSWERED,
        }
        journal.append_event("reflection", result)
        consider_thought(_as_thought(result))
        return result
    except Exception as exc:  # noqa: BLE001 — idle-cycle work is never load-bearing
        logger.debug("reflection failed: %s", exc)
        try:
            journal.append_event("reflection_skipped",
                                 {"reason": "error", "error": str(exc)[:200]})
        except Exception:  # noqa: BLE001
            pass
        return None


def drift_integrity() -> Optional[dict]:
    """Is the wake-floor drift TRENDING? Journals a non-STABLE verdict.

    The series is already being written: act/floor_calibration journals a
    `floor_drifted` event carrying {from, to} every time a harness's floor
    moves by more than its delta. Nothing had ever read it back. These are the
    readings gates/integrity.py was written for -- it just had no supplier,
    which is why it sat dormant with a docstring telling the caller to source
    the series themselves.

    Per-harness, because two harnesses drifting in opposite directions must not
    average into a calm-looking flat line.
    """
    if not _enabled():
        return None
    try:
        events = journal.read_events(limit=_DRIFT_LOOKBACK)
    except Exception as exc:  # noqa: BLE001
        logger.debug("drift_integrity: journal read failed: %s", exc)
        return None

    series: dict[str, list] = {}
    for event in events:
        if event.get("kind") != "floor_drifted":
            continue
        value = event.get("to")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            series.setdefault(str(event.get("harness") or "?"), []).append(float(value))

    worst: Optional[dict] = None
    for harness, values in series.items():
        if len(values) < MIN_INTEGRITY_SAMPLES:
            continue
        try:
            report = compute_consciousness_integrity(values)
        except Exception as exc:  # noqa: BLE001 — integrity is advisory
            logger.debug("drift_integrity: %s failed: %s", harness, exc)
            continue
        report["harness"] = harness
        if report.get("severity") in ("CRITICAL", "DRIFTING", "UNKNOWN"):
            journal.append_event("drift_integrity", {
                "harness": harness,
                "severity": report.get("severity"),
                "samples": report.get("samples"),
                "mk_p_value": report.get("mk_p_value"),
                "trend_direction": report.get("trend_direction"),
                "shift_detected": report.get("shift_detected"),
                "analysis": report.get("analysis"),
                "recommendation": report.get("recommendation"),
            })
            # CRITICAL outranks DRIFTING outranks UNKNOWN for the returned
            # summary; every one of them is journaled regardless.
            rank = {"CRITICAL": 3, "DRIFTING": 2, "UNKNOWN": 1}
            if worst is None or rank.get(report["severity"], 0) > rank.get(
                    worst.get("severity", ""), 0):
                worst = report
    return worst


__all__ = ["reflect_once", "drift_integrity"]
