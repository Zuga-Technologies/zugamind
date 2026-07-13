"""Question generator — turns a workspace trigger into a Socratic question.

Given a trigger dict + a domain (see domain_classifier.py), asks the local
model to produce:

    {
        "text":               <a single grounded question>,
        "answer_source_hint": one of "code_search" | "file_read" | "none"
    }

The hint tells answer_router.py where to look. Returns None on model
failure or an unparseable response — the caller should treat that as "no
question this round" and fall through to whatever the default behavior is;
this must never block the cycle.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger("zugamind.workspace.question_generator")

_VALID_HINTS = ("code_search", "file_read", "none")

_DOMAIN_GUIDANCE = {
    "SELF": (
        "Ask about the agent's own cognition, scanner output, or code. "
        "Prefer hints code_search or file_read."
    ),
    "OPERATIONAL": (
        "Ask about a product/business signal: revenue, customer, or deploy. "
        "Prefer hints file_read or code_search."
    ),
    "EXTERNAL": (
        "Ask about AI research, alignment, or a societal signal. "
        "Prefer hint none unless a concrete local artifact is implicated."
    ),
}

_LENS_KEYS = ("what", "why", "who", "where", "when", "how", "problem", "process", "performance")
_LEGACY_KEYS = ("kind", "type", "source", "detail", "file", "text", "path")


def _trigger_brief(trigger: dict[str, Any]) -> str:
    """One-line summary the model can chew on without drowning in payload."""
    parts: list[str] = []
    for k in _LENS_KEYS + _LEGACY_KEYS:
        v = trigger.get(k)
        if isinstance(v, str) and v:
            parts.append(f"{k}={v[:120]}")
        elif isinstance(v, (int, float, bool)) and k in _LENS_KEYS:
            parts.append(f"{k}={v}")
    return " | ".join(parts) if parts else json.dumps(trigger)[:300]


def _parse_response(raw: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction — local models tend to wrap with prose."""
    if not raw:
        return None
    m = re.search(r"\{[^{}]*\}", raw, re.DOTALL) or re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    text = obj.get("text") or obj.get("question")
    hint = obj.get("answer_source_hint") or obj.get("source") or obj.get("hint")
    if not isinstance(text, str) or not text.strip():
        return None
    if hint not in _VALID_HINTS:
        hint = "none"
    return {"text": text.strip(), "answer_source_hint": hint}


def generate_question(
    trigger: dict[str, Any],
    domain: str,
    *,
    ollama_query_fn=None,
) -> dict[str, Any] | None:
    """Return {text, answer_source_hint} or None.

    `ollama_query_fn` is injectable for tests; defaults to the live local model.
    """
    if ollama_query_fn is None:
        try:
            from cognition.models.ollama import ollama_query as ollama_query_fn  # type: ignore
        except Exception as exc:
            logger.debug("ollama import failed: %s", exc)
            return None

    dom = (domain or "SELF").upper()
    guidance = _DOMAIN_GUIDANCE.get(dom, _DOMAIN_GUIDANCE["SELF"])
    brief = _trigger_brief(trigger)

    prompt = (
        "You are the workspace's Socratic layer. Read the trigger below and "
        "produce ONE grounded question that, if answered, would change what "
        "the agent does next.\n\n"
        f"Domain: {dom}\n"
        f"{guidance}\n\n"
        f"Trigger: {brief}\n\n"
        "Rules:\n"
        " - The question must be answerable by a concrete source: code "
        "search, file read, or none.\n"
        " - Keep the question under 25 words.\n\n"
        'Respond with EXACTLY one JSON object: {"text": "<question>", '
        '"answer_source_hint": "code_search|file_read|none"}'
    )

    try:
        raw = ollama_query_fn(prompt, max_tokens=120, system="")
    except Exception as exc:
        logger.debug("ollama_query raised: %s", exc)
        return None

    q = _parse_response(raw or "")
    if q is None:
        return None
    # Answerability gate: a code_search question with zero extractable
    # keywords deterministically fails at the answer source — discard it
    # before any I/O is spent.
    if q["answer_source_hint"] == "code_search":
        try:
            from examples.socratic_reflection.answer_router import _extract_keywords
            if not _extract_keywords(q["text"], k=1):
                logger.info(
                    "[question_gen] dropped unanswerable code_search question "
                    "(no extractable keywords): %s", q["text"][:60],
                )
                return None
        except Exception as exc:
            logger.debug("answerability gate skipped: %s", exc)
    return q
