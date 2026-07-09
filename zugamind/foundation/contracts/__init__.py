"""zugamind.foundation.contracts — typed carriers for workspace decision handoffs.

Exposes the `DecisionContract` dataclass and its stdlib-only helpers.
"""

from foundation.contracts.decision_contract import (  # noqa: F401
    CONTRACT_FIELDS_HINT,
    DELIVERABLE_ACTIONS,
    RESEARCH_ACTIONS,
    DecisionContract,
    PerformanceCheck,
    ValidationResult,
    When,
    assemble,
    build_performance_check,
    classify_action,
    derive_facts,
    from_task_payload,
    to_issue_body,
    to_task_payload,
    validate,
)
