"""Domain classifier — maps a trigger to one of a small set of buckets.

Three-layer design:
  0. Lens routing (deterministic, ~0ms) — if a trigger carries structural
     "where"/"who" fields (see scanners/_template.py's optional 5W1H3P
     lenses), route on those directly.
  1. Keyword pre-filter (deterministic, ~0ms) — covers the bulk of triggers
     that name a known source or topic.
  2. Local-model fallback for ambiguous triggers (~200ms) — only fires when
     the pre-filter returns no match or a tie.

Returns: {"domain": str, "confidence": float, "method": "lens"|"keyword"|"llm"|"default"}

The three example domains below (SELF / OPERATIONAL / EXTERNAL) and their
keyword lexicons are illustrative — replace them with categories that match
your own deployment. Never raises; an unrecognized trigger returns
{"domain": "OPERATIONAL", "confidence": 0.0, "method": "default"}.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("zugamind.workspace.domain_classifier")

# ---------------------------------------------------------------------------
# Keyword lexicons — case-insensitive substring match on any string-valued
# field of the trigger dict. EXAMPLE lists only; customize per deployment.
# ---------------------------------------------------------------------------

_SELF_TOKENS = (
    "workspace", "cycle_complete", "cycle_start", "workspace_winner",
    "reflection", "scanner", "daemon", "agent_status",
    "self_prediction", "identity", "cognition", "zugamind",
)

_OPERATIONAL_TOKENS = (
    "revenue", "customer", "churn", "mrr", "arr", "subscription", "pricing",
    "gh_issue", "github_issue", "deploy", "release", "studio", "product",
    "billing", "invoice",
)

_EXTERNAL_TOKENS = (
    # Scanner type prefixes for CIV-source scanners
    "reddit_ai_post", "arxiv_paper", "hackernews_story", "ai_lab_research",
    # Source brands — papers, labs, communities
    "alignment", "agi", "openai", "anthropic", "deepmind",
    "huggingface", "hugging_face", "agent_benchmark",
    "ai_safety", "research_paper", "interpretability",
    "gemini", "mistral", "meta_ai", "google_ai",
    # AI/ML vocabulary
    "chatgpt", "llm", "gpt", "agentic", "copilot",
    "transformer", "hallucination", "prompt", "rag",
    "embedding", "inference", "fine-tun", "finetun", "vector_db",
    "machinelearning", "machine learning",
    "neural", "diffusion", "reinforcement", "llama", "qwen",
    "deepseek", "perplexity", "cohere", "mlx", "vllm", "ollama",
    "benchmark", "sota", "emergent",
)

_TOKEN_TABLE: dict[str, tuple[str, ...]] = {
    "SELF": _SELF_TOKENS,
    "OPERATIONAL": _OPERATIONAL_TOKENS,
    "EXTERNAL": _EXTERNAL_TOKENS,
}

# --- Layer 0: structural lens routing ---------------------------------------
# WHERE locus prefixes -> domain. Mirrors the optional 5W1H3P "where"/"who"
# lens fields a scanner may set (see scanners/_template.py).

_WHERE_ROUTES = (
    ("external:", "EXTERNAL"),
    ("surface:product", "OPERATIONAL"),
)

_WHO_ROUTES = (
    ("lab:", "EXTERNAL"),
    ("user:", "OPERATIONAL"),
    ("agent:", "SELF"),
)


def _lens_route(trigger: dict[str, Any]) -> str | None:
    """Deterministic domain from WHERE/WHO lenses, or None if absent."""
    where = str(trigger.get("where") or "").lower()
    if where:
        for prefix, domain in _WHERE_ROUTES:
            if where.startswith(prefix):
                return domain
    who = str(trigger.get("who") or "").lower()
    if who:
        for prefix, domain in _WHO_ROUTES:
            if who.startswith(prefix):
                return domain
    return None


_STRING_KEYS = ("source", "text", "path", "content", "kind", "subject", "summary",
                "what", "why", "problem")
_NON_TEXT_KEYS = frozenset(("where", "who", "when", "how", "how_detected"))


def _trigger_text(trigger: dict[str, Any]) -> str:
    parts: list[str] = []
    for k in _STRING_KEYS:
        v = trigger.get(k)
        if isinstance(v, str):
            parts.append(v)
    for k, v in trigger.items():
        if k in _NON_TEXT_KEYS:
            continue
        if isinstance(v, dict):
            for sub in v.values():
                if isinstance(sub, str):
                    parts.append(sub)
        elif isinstance(v, str) and v not in parts:
            parts.append(v)
    return " ".join(parts).lower()


def _keyword_score(text: str) -> dict[str, int]:
    scores = {d: 0 for d in _TOKEN_TABLE}
    if not text:
        return scores
    for domain, tokens in _TOKEN_TABLE.items():
        for tok in tokens:
            if tok in text:
                scores[domain] += 1
    return scores


def _llm_fallback(text: str) -> str | None:
    """Ask the local model to classify. Returns a domain name or None."""
    try:
        from cognition.models.ollama import ollama_query  # type: ignore
    except Exception as exc:
        logger.debug("ollama import failed (test env?): %s", exc)
        return None

    prompt = (
        "Classify the following trigger into exactly one of: "
        "SELF, OPERATIONAL, EXTERNAL.\n\n"
        "SELF = the agent's own cognition, scanners, code, identity.\n"
        "OPERATIONAL = product/business signal: revenue, customers, deploys.\n"
        "EXTERNAL = AI research, alignment, or world news.\n\n"
        f"Trigger: {text[:600]}\n\n"
        "Reply with EXACTLY one word: SELF, OPERATIONAL, or EXTERNAL."
    )
    try:
        resp = ollama_query(prompt, max_tokens=10, system="")
    except Exception as exc:
        logger.debug("ollama_query raised: %s", exc)
        return None
    if not resp:
        return None
    for d in ("SELF", "OPERATIONAL", "EXTERNAL"):
        if re.search(rf"\b{d}\b", resp.upper()):
            return d
    return None


def classify_domain(trigger: dict[str, Any], *, use_llm: bool = True) -> dict[str, Any]:
    """Map a trigger dict to one of SELF | OPERATIONAL | EXTERNAL.

    Lens routing first, then keyword pre-filter, then local-model fallback
    only when keywords return zero or a tie. Set `use_llm=False` for
    deterministic unit tests.
    """
    lens_domain = _lens_route(trigger)
    if lens_domain is not None:
        return {"domain": lens_domain, "confidence": 0.9, "method": "lens"}

    text = _trigger_text(trigger)
    scores = _keyword_score(text)
    top = max(scores.values()) if scores else 0
    winners = [d for d, s in scores.items() if s == top]

    if top > 0 and len(winners) == 1:
        total = sum(scores.values()) or 1
        return {"domain": winners[0], "confidence": round(top / total, 2), "method": "keyword"}

    if use_llm:
        llm_d = _llm_fallback(text)
        if llm_d is not None:
            return {"domain": llm_d, "confidence": 0.5, "method": "llm"}

    return {"domain": "OPERATIONAL", "confidence": 0.0, "method": "default"}
