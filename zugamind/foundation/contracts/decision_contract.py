"""ZugaMind 5W1H3P Decision Contract — the single typed carrier for every
workspace decision handoff (sentinel -> injection -> worker).

5W1H3P = **W**hat, **W**hy, **W**ho, **W**here, **W**hen, **H**ow + **P**roblem,
**P**rocess, **P**erformance.

Stdlib-only — dataclasses + uuid + json, NO pydantic, no third-party deps.
This module is the source of truth every handoff imports.

Field ownership:
    code derives  -> who, where, when           (derive_facts)
    model judges  -> what, why, how, problem, process
    code builds   -> performance                (build_performance_check)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


# 5W1H3P judgment fields the local model fills. Appended to the sentinel's
# decision schema so every decision converges on one contract carrier.
CONTRACT_FIELDS_HINT = """
Additionally include these 5W1H3P contract fields (the carrier the worker reads):
  "what": "the one concrete action (exactly one)",
  "why": "observed evidence — cite the trigger above, do not free-associate",
  "how": "the command/edit PLUS its rollback",
  "problem": "root cause as a single falsifiable claim",
  "process": "ordered steps: one change -> verify -> next"
"""

# ---------------------------------------------------------------------------
# Action classification
#
# CANONICAL deliverable core — single source of truth. `research` is its own
# class here: it SPENDS (earns a paid-tier call) but its Performance check is
# a soft artifact (a research note / proposal), not a runnable pytest/health
# probe.
# ---------------------------------------------------------------------------
DELIVERABLE_ACTIONS = frozenset(
    {"code", "fix_code", "restart_service", "restart_after_pull", "investigate_prod"}
)
RESEARCH_ACTIONS = frozenset({"research"})
# Everything else (analyze, reflect, log, alert, none, remediate, ...) is
# cognition — a reflect/analyze thought, not a real-state change.


def classify_action(action: str) -> str:
    """Derive the action_class that drives tiered enforcement.

    Returns one of: "deliverable" | "research" | "cognition".
    """
    a = (action or "").strip().lower()
    if a in DELIVERABLE_ACTIONS:
        return "deliverable"
    if a in RESEARCH_ACTIONS:
        return "research"
    return "cognition"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------
@dataclass
class When:
    """The WHEN guard. `defer=True` parks the cycle (mid-boot / mid-swap)."""

    defer: bool = False
    reason: str = "clear"


@dataclass
class PerformanceCheck:
    """A machine- or soft-verifiable "done" keyed by action class.

    `runnable=True`  -> a real probe (pytest target / health curl / prod HTTP).
    `runnable=False` -> a soft artifact (note / research/proposal thread).
    """

    kind: str  # "pytest" | "health" | "http" | "soft"
    target: str
    runnable: bool
    expect: str = ""


@dataclass
class ValidationResult:
    """Outcome of `validate()`. `decision` is the routing verb the caller acts on."""

    ok: bool
    decision: str  # "inject" | "rest"
    reason: str
    missing: tuple = ()


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------
@dataclass
class DecisionContract:
    """The single carrier for a workspace decision. Every handoff imports this.

    Continuity: `corr_id` ties the decision to its receipt + the calibration
    record; `goal_id` links it to the active goal's working thread.
    """

    # --- 5W1H3P -------------------------------------------------------------
    what: str  # model: the one action (exactly one)
    why: str  # model: observed evidence — must cite the trigger
    who: str  # code:  service/owner
    where: str  # code:  exact path / host / port / repo
    when: When  # code:  defer guard
    how: str  # model: command/edit PLUS rollback
    problem: str  # model: root cause as a falsifiable claim
    process: str  # model: ordered steps, one-change -> verify -> next
    performance: PerformanceCheck  # code-built: keyed by action_class

    # --- metadata -----------------------------------------------------------
    action: str  # the verb
    action_class: str = ""  # derived in __post_init__ when blank
    corr_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    goal_id: Optional[str] = None
    tier: str = "local"  # telemetry only, from the attention/auction step
    # Auction key the value-prior re-weights on (winner.source_module + trigger
    # type). Carried so reconcile can record the authoritative value score on
    # the SAME key next auction reads. Telemetry/value-loop only — not 5W1H3P.
    source_module: str = ""
    trigger_type: str = ""

    def __post_init__(self) -> None:
        if not self.action_class:
            self.action_class = classify_action(self.action)


# ---------------------------------------------------------------------------
# Population pipeline:
#   derive_facts -> sentinel(model fields) -> build_performance_check -> assemble
# ---------------------------------------------------------------------------
def derive_facts(trigger: dict, winner: Any, state: dict) -> dict:
    """Code-derived 5W facts: who / where / when. No model call.

    Best-effort + defensive — a sparse trigger still yields a usable contract;
    incomplete fields are caught later by `validate()` for the deliverable class.
    Returns ``{"who", "where", "when"}`` ready to splat into a DecisionContract.
    """
    trigger = trigger or {}
    state = state or {}

    service = (trigger.get("service") or "").strip()
    module = getattr(winner, "source_module", "") or ""
    who = service or module or "unknown"

    # where: most specific locator available, in path/host/port order
    path = trigger.get("file") or trigger.get("detail")
    host = trigger.get("host")
    port = trigger.get("port")
    where_parts: list[str] = []
    if path:
        where_parts.append(str(path))
    if host:
        where_parts.append(f"host={host}")
    if port:
        where_parts.append(f"port={port}")
    where = " ".join(where_parts) if where_parts else (service or "unknown")

    # when: defer (rest this cycle) if the runtime is mid-boot / mid-swap
    defer = bool(state.get("mid_boot") or state.get("mid_swap") or state.get("defer"))
    if not defer:
        when = When(defer=False, reason="clear")
    elif state.get("mid_boot"):
        when = When(defer=True, reason="mid-boot guard active")
    elif state.get("mid_swap"):
        when = When(defer=True, reason="mid-swap guard active")
    else:
        when = When(defer=True, reason=str(state.get("defer_reason") or "deferred by state"))

    return {"who": who, "where": where, "when": when}


def build_performance_check(action: str, *, where: str = "", target_hint: str = "") -> PerformanceCheck:
    """Code-built Performance check, keyed by action class.

    deliverable -> runnable (pytest / health / http).
    research / cognition -> soft artifact.

    `target_hint` lets the caller pin a concrete probe (e.g. a specific
    failing-test path); absent it, a class-appropriate default is used.
    """
    cls = classify_action(action)
    a = (action or "").strip().lower()

    if a in ("code", "fix_code"):
        return PerformanceCheck(
            kind="pytest",
            target=target_hint or _pytest_target_for(where),
            runnable=True,
            expect="exit 0 (failing test flips to passing)",
        )
    if a in ("restart_service", "restart_after_pull"):
        return PerformanceCheck(
            kind="health",
            target=target_hint or where or "health endpoint",
            runnable=True,
            expect="HTTP 200 / LISTEN on expected port",
        )
    if a == "investigate_prod":
        return PerformanceCheck(
            kind="http",
            target=target_hint or where or "prod endpoint",
            runnable=True,
            expect="expected status",
        )
    if cls == "research":
        return PerformanceCheck(
            kind="soft",
            target=target_hint or "research note / proposal thread",
            runnable=False,
            expect="artifact exists",
        )
    return PerformanceCheck(
        kind="soft",
        target=target_hint or "note / proposal thread id",
        runnable=False,
        expect="artifact exists",
    )


def _pytest_target_for(where: str) -> str:
    """Best-effort: name a pytest target from a 'where' locator.

    The concrete target is pinned at wiring time (the specific failing test);
    here we surface the changed module so a runnable check is never empty.
    """
    token = (where or "").strip().split(" ")[0] if where else ""
    if token.endswith(".py"):
        return f"pytest target for {token}"
    return "pytest (changed-module target)"


def assemble(
    *,
    action: str,
    model_fields: dict,
    trigger: dict,
    winner: Any,
    state: dict,
    goal_id: Optional[str] = None,
    tier: str = "local",
    corr_id: Optional[str] = None,
) -> DecisionContract:
    """Run the full pipeline: derive_facts + model fields + performance check.

    `model_fields` carries the sentinel's judgment fields
    (what/why/how/problem/process and optional performance_target).
    """
    facts = derive_facts(trigger, winner, state)
    perf = build_performance_check(
        action, where=facts["where"], target_hint=model_fields.get("performance_target", "")
    )
    return DecisionContract(
        source_module=getattr(winner, "source_module", "") or "",
        trigger_type=(trigger or {}).get("type", "") or "",
        what=model_fields.get("what", action),
        why=model_fields.get("why", ""),
        who=facts["who"],
        where=facts["where"],
        when=facts["when"],
        how=model_fields.get("how", ""),
        problem=model_fields.get("problem", ""),
        process=model_fields.get("process", ""),
        performance=perf,
        action=action,
        goal_id=goal_id,
        tier=tier,
        corr_id=corr_id or uuid.uuid4().hex,
    )


# ---------------------------------------------------------------------------
# Tiered enforcement
# ---------------------------------------------------------------------------
_BASE_REQUIRED = ("what", "why", "who", "where")
_FULL_REQUIRED = ("what", "why", "who", "where", "how", "problem", "process")


def _missing_fields(contract: DecisionContract, names: tuple) -> tuple:
    out = []
    for n in names:
        v = getattr(contract, n, None)
        if v is None or (isinstance(v, str) and not v.strip()):
            out.append(n)
    return tuple(out)


def validate(contract: DecisionContract) -> ValidationResult:
    """Tiered, fail-closed enforcement.

    deliverable: all 7 text fields present AND a RUNNABLE performance check,
                 else fail-closed -> rest.
    research:    all 7 text fields present; SOFT performance check accepted
                 (research spends, but cannot always produce a runnable probe).
    cognition:   what/why/who/where present; soft check accepted -> proceed.

    A mid-boot/mid-swap `when.defer` parks the cycle regardless of class.
    """
    cls = contract.action_class or classify_action(contract.action)

    # defer guard first — applies to every class
    if contract.when and contract.when.defer:
        return ValidationResult(False, "rest", f"deferred: {contract.when.reason}")

    if cls == "cognition":
        missing = _missing_fields(contract, _BASE_REQUIRED)
        if missing:
            return ValidationResult(False, "rest", f"cognition missing {','.join(missing)}", missing)
        return ValidationResult(True, "inject", "cognition ok (soft check)")

    # deliverable + research require the full field set
    missing = _missing_fields(contract, _FULL_REQUIRED)
    if contract.performance is None:
        missing = missing + ("performance",)
    if missing:
        return ValidationResult(False, "rest", f"{cls} missing {','.join(missing)}", missing)

    if cls == "deliverable":
        if not (contract.performance and contract.performance.runnable):
            return ValidationResult(False, "rest", "deliverable requires a runnable performance check")
        return ValidationResult(True, "inject", "deliverable ok (runnable check)")

    # research: spends but soft check accepted
    return ValidationResult(True, "inject", "research ok (soft check, spends)")


# ---------------------------------------------------------------------------
# Serialization (to_task_payload for injection, to_issue_body for a bridge)
# ---------------------------------------------------------------------------
def to_task_payload(contract: DecisionContract) -> dict:
    """Serialize all 9 fields + metadata for a task-injection consumer.

    The returned dict is the canonical carrier a downstream worker embeds; it
    threads `corr_id` so the receipt + calibration record join on it.
    """
    return {
        "action": contract.action,
        "action_class": contract.action_class,
        "corr_id": contract.corr_id,
        "goal_id": contract.goal_id,
        "tier": contract.tier,
        "source_module": contract.source_module,
        "trigger_type": contract.trigger_type,
        "what": contract.what,
        "why": contract.why,
        "who": contract.who,
        "where": contract.where,
        "when": {"defer": contract.when.defer, "reason": contract.when.reason},
        "how": contract.how,
        "problem": contract.problem,
        "process": contract.process,
        "performance": {
            "kind": contract.performance.kind,
            "target": contract.performance.target,
            "runnable": contract.performance.runnable,
            "expect": contract.performance.expect,
        },
    }


def from_task_payload(payload: dict) -> DecisionContract:
    """Inverse of `to_task_payload` — rebuild a contract from its serialized form."""
    payload = payload or {}
    w = payload.get("when") or {}
    p = payload.get("performance") or {}
    return DecisionContract(
        what=payload.get("what", ""),
        why=payload.get("why", ""),
        who=payload.get("who", ""),
        where=payload.get("where", ""),
        when=When(defer=bool(w.get("defer")), reason=w.get("reason", "clear")),
        how=payload.get("how", ""),
        problem=payload.get("problem", ""),
        process=payload.get("process", ""),
        performance=PerformanceCheck(
            kind=p.get("kind", "soft"),
            target=p.get("target", ""),
            runnable=bool(p.get("runnable")),
            expect=p.get("expect", ""),
        ),
        action=payload.get("action", ""),
        action_class=payload.get("action_class", ""),
        corr_id=payload.get("corr_id") or uuid.uuid4().hex,
        goal_id=payload.get("goal_id"),
        tier=payload.get("tier", "local"),
        source_module=payload.get("source_module", ""),
        trigger_type=payload.get("trigger_type", ""),
    )


def to_issue_body(contract: DecisionContract) -> str:
    """Render the contract as a GitHub issue body for an automated-fix bridge.

    The consumer's `build_prompt` reads this; the Performance check below IS
    the failing test it must write and flip to passing.
    """
    perf = contract.performance
    runnable = "runnable" if (perf and perf.runnable) else "soft artifact"
    when_str = f"DEFER — {contract.when.reason}" if contract.when.defer else "now"
    lines = [
        f"**ZugaMind decision** `{contract.action}` ({contract.action_class}) — "
        f"corr_id `{contract.corr_id}`",
        "",
        "### 5W1H3P contract",
        f"- **What:** {contract.what}",
        f"- **Why:** {contract.why}",
        f"- **Who:** {contract.who}",
        f"- **Where:** {contract.where}",
        f"- **When:** {when_str}",
        f"- **How:** {contract.how}",
        f"- **Problem:** {contract.problem}",
        f"- **Process:** {contract.process}",
        "",
        "### Performance check (= the failing test to write)",
        f"- **Kind:** {perf.kind} ({runnable})",
        f"- **Target:** {perf.target}",
        f"- **Expect:** {perf.expect}",
    ]
    if contract.goal_id:
        lines += ["", f"_goal_id: {contract.goal_id}_"]
    lines += ["", f"<!-- zugamind-corr-id: {contract.corr_id} -->"]
    return "\n".join(lines)
