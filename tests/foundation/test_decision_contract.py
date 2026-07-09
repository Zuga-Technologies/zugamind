"""Tests for the 5W1H3P decision contract (foundation/contracts/decision_contract.py).

Covers:
  - classify_action: the three-class model (deliverable / research / cognition),
    including `research` as its own spends-but-soft class.
  - derive_facts: code-derived who / where / when population from a trigger.
  - build_performance_check: the right check kind per action class.
  - validate: tiered enforcement (deliverable blocks without a runnable check;
    research spends with a soft check; cognition proceeds; defer parks the cycle).
  - round-trip to_task_payload / from_task_payload and to_issue_body shape.
"""

import types

from foundation.contracts.decision_contract import (
    PerformanceCheck,
    When,
    assemble,
    build_performance_check,
    classify_action,
    derive_facts,
    from_task_payload,
    to_issue_body,
    to_task_payload,
    validate,
    DecisionContract,
)


def _winner(module="code_changes"):
    return types.SimpleNamespace(source_module=module)


def _full_contract(action="code", **over):
    """A complete, valid contract for the given action (overridable per field)."""
    base = dict(
        what=over.pop("what", "fix the failing import in agent.py"),
        why=over.pop("why", "trigger errors: ImportError on cold boot"),
        who=over.pop("who", "worker daemon"),
        where=over.pop("where", "example_repo/agent.py"),
        when=over.pop("when", When(defer=False)),
        how=over.pop("how", "add the missing import; rollback = git checkout agent.py"),
        problem=over.pop("problem", "module imported before it is defined"),
        process=over.pop("process", "1) add import 2) run pytest 3) confirm green"),
        performance=over.pop("performance", build_performance_check(action, where="agent.py")),
        action=action,
    )
    base.update(over)
    return DecisionContract(**base)


# --- classify_action ---------------------------------------------------------
def test_classify_deliverable():
    for a in ("code", "fix_code", "restart_service", "restart_after_pull", "investigate_prod"):
        assert classify_action(a) == "deliverable", a


def test_classify_research_is_its_own_class():
    assert classify_action("research") == "research"


def test_classify_cognition_default():
    for a in ("analyze", "reflect", "log", "alert", "none", "", "garbled"):
        assert classify_action(a) == "cognition", a


# --- derive_facts --------------------------------------------------------------
def test_derive_facts_who_where_from_trigger():
    trigger = {"service": "example-api", "detail": "LOCAL example-api down", "port": 8001}
    facts = derive_facts(trigger, _winner(), {})
    assert facts["who"] == "example-api"
    assert "LOCAL example-api down" in facts["where"]
    assert "port=8001" in facts["where"]
    assert facts["when"].defer is False


def test_derive_facts_falls_back_to_winner_module():
    facts = derive_facts({}, _winner("priority_goals"), {})
    assert facts["who"] == "priority_goals"
    assert facts["where"] == "unknown"


def test_derive_facts_defer_on_mid_boot():
    facts = derive_facts({"service": "x"}, _winner(), {"mid_boot": True})
    assert facts["when"].defer is True
    assert "boot" in facts["when"].reason


# --- build_performance_check ---------------------------------------------------
def test_perf_check_code_is_runnable_pytest():
    pc = build_performance_check("code", where="backend/x.py")
    assert pc.kind == "pytest" and pc.runnable is True


def test_perf_check_restart_is_runnable_health():
    pc = build_performance_check("restart_service", where="port=8000")
    assert pc.kind == "health" and pc.runnable is True


def test_perf_check_investigate_is_runnable_http():
    pc = build_performance_check("investigate_prod", where="https://example.com/health")
    assert pc.kind == "http" and pc.runnable is True


def test_perf_check_research_is_soft():
    pc = build_performance_check("research")
    assert pc.kind == "soft" and pc.runnable is False


def test_perf_check_cognition_is_soft():
    pc = build_performance_check("analyze")
    assert pc.kind == "soft" and pc.runnable is False


def test_perf_check_target_hint_pins_concrete_probe():
    pc = build_performance_check("fix_code", target_hint="failing-test #42")
    assert pc.target == "failing-test #42" and pc.runnable is True


# --- validate: tiered enforcement -----------------------------------------------
def test_validate_deliverable_full_runnable_injects():
    res = validate(_full_contract("code"))
    assert res.ok and res.decision == "inject"


def test_validate_deliverable_missing_field_rests():
    res = validate(_full_contract("code", problem=""))
    assert not res.ok and res.decision == "rest"
    assert "problem" in res.missing


def test_validate_deliverable_soft_check_rests():
    soft = PerformanceCheck(kind="soft", target="note", runnable=False)
    res = validate(_full_contract("code", performance=soft))
    assert not res.ok and res.decision == "rest"
    assert "runnable" in res.reason


def test_validate_research_spends_with_soft_check():
    c = _full_contract("research")
    assert c.action_class == "research"
    assert c.performance.runnable is False
    res = validate(c)
    assert res.ok and res.decision == "inject"


def test_validate_research_missing_field_rests():
    res = validate(_full_contract("research", how=""))
    assert not res.ok and res.decision == "rest"


def test_validate_cognition_base_fields_inject():
    c = _full_contract("analyze", how="", problem="", process="")
    res = validate(c)
    assert res.ok and res.decision == "inject"


def test_validate_cognition_missing_why_rests():
    res = validate(_full_contract("analyze", why="", how="", problem="", process=""))
    assert not res.ok and res.decision == "rest"
    assert "why" in res.missing


def test_validate_defer_parks_any_class():
    res = validate(_full_contract("code", when=When(defer=True, reason="mid-swap guard active")))
    assert not res.ok and res.decision == "rest"
    assert "defer" in res.reason


# --- assemble ------------------------------------------------------------------
def test_assemble_produces_valid_deliverable():
    trigger = {"service": "example-api", "file": "example_repo/foo.py", "port": 8001}
    model_fields = {
        "what": "fix foo",
        "why": "trigger: foo crashes",
        "how": "patch; rollback git checkout",
        "problem": "null deref",
        "process": "1 fix 2 test",
    }
    c = assemble(action="code", model_fields=model_fields, trigger=trigger, winner=_winner(), state={})
    assert c.action_class == "deliverable"
    assert c.who == "example-api"
    assert "foo.py" in c.where
    assert validate(c).ok


# --- serialization round-trips --------------------------------------------------
def test_task_payload_round_trip_preserves_all_fields():
    c = _full_contract("code", goal_id="goal-7", tier="sonnet")
    payload = to_task_payload(c)
    for k in ("what", "why", "who", "where", "when", "how", "problem", "process", "performance"):
        assert k in payload
    back = from_task_payload(payload)
    assert back.what == c.what
    assert back.corr_id == c.corr_id
    assert back.goal_id == "goal-7"
    assert back.when.defer == c.when.defer
    assert back.performance.runnable == c.performance.runnable
    assert back.action_class == "deliverable"


def test_assemble_carries_value_prior_key():
    c = assemble(action="code", model_fields={}, trigger={"type": "code_changes"},
                 winner=_winner("daemon"), state={})
    assert c.source_module == "daemon" and c.trigger_type == "code_changes"
    back = from_task_payload(to_task_payload(c))
    assert back.source_module == "daemon" and back.trigger_type == "code_changes"


def test_issue_body_carries_corr_id_and_perf_target():
    c = _full_contract("code", goal_id="goal-9")
    body = to_issue_body(c)
    assert c.corr_id in body
    assert "Performance check" in body
    assert c.performance.target in body
    assert "goal-9" in body
