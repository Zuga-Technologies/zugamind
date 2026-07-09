"""Share-worthiness filter for candidate outbound thoughts.

Runs before a thought is surfaced to any outbound channel, to suppress
low-conviction, off-topic, or non-actionable thoughts before they ever reach
a delivery path.

Downstream consumers (e.g. a human-notification channel) should apply their
own rate-limiting and dedup; this filter only decides worth-sharing, not
delivery.

Stdlib only.
"""
from __future__ import annotations

from typing import Any

WHITELIST = frozenset({
    "concern",
    "proposal",
    "insight",
    "question",
    "cognition_mod",
    "business_idea",
})

CONFIDENCE_FLOOR = 0.6


def should_share(thought: dict[str, Any]) -> tuple[bool, str]:
    """Decide if a candidate thought is share-worthy.

    Returns (share, reason). reason is always populated -- caller logs it.
    """
    text = (thought.get("text") or "").strip()
    if not text:
        return False, "empty_text"

    try:
        confidence = float(thought.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < CONFIDENCE_FLOOR:
        return False, f"low_confidence:{confidence:.2f}"

    topic_class = thought.get("topic_class") or ""
    if topic_class not in WHITELIST:
        return False, f"topic_off_whitelist:{topic_class}"

    proposed = (thought.get("proposed_action") or "").strip()
    ask_text = (thought.get("ask_text") or text).strip()
    if not (proposed or ask_text.endswith("?")):
        return False, "no_action_no_question"

    return True, "share"
