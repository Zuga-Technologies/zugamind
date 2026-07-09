"""LLM-judge — final backstop for narrative confabulation.

The heuristic gates (work_claim verb<->commit matching, entity grounding) catch
the bulk of fabricated-accomplishment posts cheaply. This is the last line: a
local-model judge that reads the post AND the ground-truth evidence (recent
commits) and decides ALLOW / SUPPRESS. It catches confabs the heuristics miss —
novel phrasings, claims about real entities with no backing work — at the cost
of one local (free) inference per claim-bearing narrative post.

NOT a truth oracle: the local model is small and can err in both directions.
It is a probabilistic backstop, not a guarantee. Stdlib-only, FAIL-OPEN — any
error or unparseable verdict returns ALLOW so the gate can never silence the
agent on its own malfunction.
"""
from __future__ import annotations

import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a strict fact-checker for an AI agent's self-reports. You are given "
    "the agent's draft post and the ONLY evidence of what it actually did this "
    "period (recent git commit subjects). Decide if the post claims completed "
    "work, integrations, or setup that the evidence does NOT support.\n"
    "Rules:\n"
    "- A claim of having done/built/integrated/set-up something needs matching "
    "evidence. No matching commit => unsupported.\n"
    "- Future plans, questions, and hedged intentions ('plan to', 'considering') "
    "are fine — they are not claims of completed work.\n"
    "- Naming an external tool/product as already in use, with no evidence, is "
    "unsupported.\n"
    "Answer with exactly one word on the first line: ALLOW or SUPPRESS. "
    "Then one short line of reason."
)


def judge_post(text: str, commits: Optional[List[str]] = None,
               window_minutes: int = 30) -> dict:
    """Return {"verdict": "ALLOW"|"SUPPRESS", "reason": str}. Fail-open (ALLOW)."""
    try:
        if not text or not text.strip():
            return {"verdict": "ALLOW", "reason": "empty"}

        if commits is None:
            try:
                from gates.work_claim import _recent_commits, _repo_root
                root = _repo_root()
                commits = _recent_commits(window_minutes, root) if root else []
            except Exception:
                commits = []

        evidence = "\n".join(f"- {c}" for c in commits) or "(no commits this period)"
        prompt = (
            f"EVIDENCE — what actually happened (git commits, last {window_minutes}min):\n"
            f"{evidence}\n\n"
            f"DRAFT POST:\n{text[:1500]}\n\n"
            f"Verdict (ALLOW or SUPPRESS) + reason:"
        )

        from cognition.models.ollama import ollama_query
        resp = ollama_query(prompt, max_tokens=120, system=_SYSTEM)
        if not resp:
            return {"verdict": "ALLOW", "reason": "judge_unavailable"}

        head = resp.strip().splitlines()[0].upper()
        reason = " ".join(resp.strip().splitlines()[1:])[:160]
        if "SUPPRESS" in head:
            return {"verdict": "SUPPRESS", "reason": reason or "judge: unsupported claim"}
        return {"verdict": "ALLOW", "reason": reason or "judge: ok"}
    except Exception as e:
        logger.debug("llm_judge failed (fail-open): %s", e)
        return {"verdict": "ALLOW", "reason": "judge_error"}
