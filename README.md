# ZugaMind

ZugaMind is a stdlib-only Global Workspace Theory (GWT) cognitive substrate
for autonomous agents that routes deliberate work to Claude. It gives an
agent an explicit, inspectable attention mechanism: independent modules
submit salience bids every cycle, an attention schema modulates them for
health (no stuck loops, no starved modules, no monoculture), exactly one
winner is selected and broadcast, and — only when the winner's work
genuinely warrants it — the workspace hands off to Claude through a
fail-closed, budget-clamped gate. Zero pip dependencies in the core package;
`pytest` is the only development dependency.

Built by Zuga Technologies. This is independent research and engineering,
not affiliated with or endorsed by Anthropic.

## Why now

On 2026-07-06, Anthropic published ["A global workspace in language
models"](https://transformer-circuits.pub/2026/workspace) describing
evidence that Claude's own internal computation exhibits a global-workspace-like
structure: a limited-capacity bottleneck, broadcast to the rest of the
network, and competition among candidate representations for access to it.
The paper is explicit that **what decides admission to that workspace is
still unknown** — the mechanism is emergent, discovered after the fact by
interpretability tooling, not designed in.

ZugaMind is the engineered complement. It doesn't look inside a model's
activations; it implements the same *pattern* — one bottleneck, one winner
per cycle, broadcast, competition — as an external, steerable substrate that
sits in front of an LLM and decides when that LLM gets invoked at all. Where
Claude's workspace is emergent and hard to steer, ZugaMind's is engineered
and steerable: admission is decided by an explicit salience-bidding
mechanism plus an attention-schema self-model, both fully logged, both
extensible via a plain Python callback (see `register_modulator` below).
The one open question the paper names — "what decides admission" — is the
one thing ZugaMind answers explicitly.

| Paper's GWT observation | ZugaMind's mechanism |
|---|---|
| Limited capacity — one winner per cycle | `Workspace.run_cycle()` selects exactly one `SalienceBid` per cycle |
| Broadcast to the rest of the network | `WorkspaceModule.on_broadcast()` fires on every registered module every cycle |
| Competition for access | Modules submit `SalienceBid`s; `AttentionSchema.modulate()` re-weights them before selection |
| Admission mechanism unknown / not steerable | `AttentionSchema` (streak dampening, diversity cap, blind-spot boost, novelty bonus) + pluggable `register_modulator()` hooks — explicit and inspectable |
| Reportability | `Workspace.get_stats()` returns the full bid field, the winner, and the attention self-model every single cycle |
| Deliberate vs. automatic processing | Cheap/free local-model tier for routine cycles; the fail-closed action gate escalates to Claude only when a winner's work justifies the spend |

## Architecture

```
  scanners (perception)
       |
       v
  salience bids  <---- each WorkspaceModule.generate_bid()
       |
       v
  attention schema  ---- streak dampening, diversity cap,
       |                 blind-spot boost, novelty bonus
       v
  ONE winner  (salience**power weighted selection)
       |
       v
  broadcast  ---- every module's on_broadcast() fires
       |
       v
  workspace planner  ---- winner -> a short task plan
       |
       v
  action gate (gates/action_gate.py)  ---- fail-closed, budget-clamped
       |                                    content screen + human veto
       v
  Claude  (or the free local-model tier, if the task doesn't warrant it)
```

Every arrow above is inspectable: `Workspace.get_stats()` after any cycle
returns every bid that competed, the winner, the runner-up, and the
attention schema's current self-model (recent foci, blind spots, whether
it's stuck, attention-switch count).

## Quickstart

No API key required:

```bash
git clone <this-repo>
cd zugamind-oss
python demo.py
```

This registers the shipped example modules (`zugamind/cognition/workspace/workspace_modules.py`),
feeds them synthetic scanner triggers for 8 cycles, and prints every bid,
the winner, and the proposed plan per cycle — using only the free local
tier (no network, no key). If `ANTHROPIC_API_KEY` is set in your
environment, the final cycle's winner is additionally routed through the
real action gate to Claude; otherwise that step runs in `dry_run=True` mode
(no network call, no spend) so the whole demo works offline out of the box.

```bash
python demo.py --cycles 20 --seed 3      # more cycles, different synthetic run
```

Run the test suite:

```bash
pip install -e ".[dev]"
pytest
```

## Safety design

ZugaMind assumes an autonomous agent will eventually be wrong, and designs
for that instead of assuming it away:

- **Fail-closed gates.** `gates/action_gate.py` is the single doorway from
  the workspace to Claude. Any missing or erroring check — budget
  resolution, model routing, the content screen — returns `ok=False`.
  Nothing silently proceeds on a gate malfunction.
- **A content screen, not just a spend limit.** Before any paid call,
  `screen_intent()` regex-blocks prompt-injection phrasing, destructive
  shell/SQL commands, forced pushes, secret-exfiltration attempts, and
  attempts to edit the gate's own safety-critical files.
- **A hard budget cap.** `foundation/budget.py` enforces a monthly USD
  ceiling (`ZUGAMIND_MONTHLY_BUDGET_USD`, default $10). The free local-model
  tier is never gated on remaining budget — a budget outage can freeze
  *spend*, never *thinking*.
- **A human veto point.** Any intent can be marked `requires_human: True`;
  the gate refuses to execute it at all. Wiring an actual notification
  (Discord, Slack, email, a ticket) onto that refusal is left to the
  integrator — the core guarantees the refusal, not the paging.
- **Post-hoc integrity checks**, layered on top of the pre-action gate:
  - `gates/work_claim.py` — verifies the agent's own accomplishment claims
    against real git history; a claim with no matching commit is flagged as
    confabulation, regardless of how confidently it's phrased.
  - `gates/value_gate.py` — scores whether a past action actually changed
    real state, and dampens the salience of bid types that historically
    didn't pay off (ships disabled by default; opt in via
    `ZUGAMIND_VALUE_GATE_ENABLED=true`).
  - `gates/operational_truth.py` — a freshness gate that re-probes live
    state before a claim is allowed to surface, so a true-once observation
    can't be re-narrated as still-true indefinitely.
  - `gates/zugashield.py` + `gates/integrity.py` — a two-timescale
    misevolution detector: per-cycle drift-from-baseline (GREEN/YELLOW/RED,
    RED writes a kill-switch `PAUSE` file) plus a longitudinal
    Dickey-Fuller stationarity test (pure stdlib, no numpy) that catches
    slow drift too gradual to trip the per-cycle threshold.
  - `gates/self_mod_cooldown.py` — a restart-durable, disk-backed cooldown
    so a self-modification proposal can't thrash the same file repeatedly.
- **Fully logged.** `Workspace.get_stats()`, the attention schema's
  `get_context()`, and every gate's telemetry are structured and
  loggable every cycle — "why did it do that" should always be answerable
  from the log, not from re-running the model.

## Limitations

- This is a **macro-scale task workspace** — an external orchestration
  layer that decides what an agent attends to and when it escalates to an
  LLM. It is **not** a claim about, model of, or replacement for the
  internal, emergent workspace Anthropic's interpretability research
  describes inside the model's own computation. The paper mapping above is
  an analogy at the pattern level, not an implementation of the same
  mechanism.
- The example modules in `cognition/workspace/workspace_modules.py` and the
  three world-scanners in `scanners/world/` are illustrative, not a
  production perception stack. Replace them with your own.
- The budget model (`foundation/budget.py`) is a simple standalone monthly
  cap by design — it is not a multi-agent fleet-wide accounting system.
  Integrators running several agents against a shared budget should supply
  their own `monthly_cap()`.
- `gates/action_gate.py`'s content screen is a regex-based acute safety net,
  not a general-purpose alignment solution — it catches clear-cut,
  named failure classes, not everything that could go wrong.

## License

Apache 2.0 — see `LICENSE`.
