# ZugaMind

Your agent harness thinks when you prompt it. ZugaMind thinks the rest of
the time.

ZugaMind is a stdlib-only Global Workspace Theory (GWT) cognitive substrate
that runs as a persistent sidecar next to an agent harness — Claude Code,
OpenClaw, Hermes, Codex CLI, or anything else that runs as a CLI process.
A harness is reactive: it wakes up when you type a prompt, works for one
turn, and forgets everything the moment that turn ends. ZugaMind doesn't.
It runs always-on, perceives the world through scanners, holds continuity
across those wakes in an episodic journal, and — only when something
genuinely wins its attention and clears a fail-closed safety gate — reaches
out and WAKES your harness with a briefing describing what happened and
what to do about it.

Underneath the sidecar behavior is the same explicit, inspectable attention
mechanism this project started as: independent modules submit salience bids
every cycle, an attention schema modulates them for health (no stuck loops,
no starved modules, no monoculture), exactly one winner is selected and
broadcast, and — only when the winner's work genuinely warrants it — the
workspace hands off to Claude through a fail-closed, budget-clamped gate.
Zero pip dependencies in the core package; `pytest` is the only development
dependency.

Built by Zuga Technologies. This is independent research and engineering,
not affiliated with or endorsed by Anthropic. To be precise about what this
is: ZugaMind upgrades the AGENT — giving it persistence, attention, and
proactivity a stateless harness invocation doesn't have on its own — it
never claims to upgrade, replace, or model the underlying LLM itself.

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

## Always-on: the latch-on model

Everything above (the workspace, the attention schema, the action gate)
decides *when something deserves attention*. Three more pieces close the
loop from "decided" to "your harness is now working on it":

- **`continuity/journal.py`** — an episodic, append-only log
  (`data/engine/journal.jsonl`) of every notable cycle event: workspace
  winners, harness invocations, alarms, quiet-hours deferrals, handoffs.
  `build_briefing()` turns the tail of that log into the markdown a waking
  harness reads: current cognitive state, time since the last wake, why
  it's being woken *this* time, what happened since the last wake grouped
  by kind, and anything left unresolved. The briefing is hard-capped (~4000
  chars, `ZUGAMIND_BRIEFING_MAX_CHARS`) and trims its oldest entries first
  when there's too much to say — context assembly is exactly where
  ambient-cognition systems go wrong, drowning the model in noise instead
  of orienting it.
- **`act/command_actuator.py`** — the harness adapter. Given an
  already-approved decision and a briefing, it writes the briefing to a
  temp file, substitutes that path into a configured argv
  (`{briefing_file}`), and runs it as a subprocess — rate-limited per
  harness on both a rolling hour and a rolling day, and never raising (a
  bad command, a timeout, a missing binary all come back as a plain
  `{"ok": False, "error": ...}`, never an exception). It also understands
  an optional quiet-hours window (`ZUGAMIND_QUIET_HOURS`, or a
  `"quiet_hours"` block in the harness config file) that a caller can use
  to suppress wakes overnight.
- **`stream/runner.py`** — the always-on loop:
  `python runner.py --daemon` (from the repo root). Each cycle it sweeps scanners, runs
  one workspace cycle, transitions the cognitive state machine, journals
  what happened — perception and journaling never stop, quiet hours or
  not — and, only if there's a winner AND `gates/action_gate.py` approves
  AND it isn't currently quiet hours, hands the briefing to every enabled,
  configured harness via `command_actuator`. A winner that arrives during
  quiet hours is deferred (journaled, not lost) and surfaces in the
  briefing the next time a real wake happens.

This is the "latch-on": ZugaMind attaches to your harness of choice as a
sidecar process, not a fork or a plugin. Your harness's own code never
changes; it just receives an occasional, well-justified wake-up call with
full context for what to do next.

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

## Works with your harness

`act/command_actuator.py` loads harness configs from JSON (default
`zugamind/data/harness.json`, overridable via `ZUGAMIND_HARNESS_CONFIG`).
`examples/harness-configs/` ships one ready-to-copy file per harness.

Every row below marked **verified end-to-end** passed the same live test on
2026-07-08 (`scripts/verify_harness.py`, nothing mocked): a canary trigger
won the workspace, cleared the action gate, the actuator spawned the real
harness binary, and the woken agent read ZugaMind's briefing and echoed the
canary token back.

| Harness | Config | Status |
|---|---|---|
| [Claude Code](https://claude.com/claude-code) 2.1.204 | `examples/harness-configs/claude-code.json` | **Verified end-to-end** (Windows) |
| [OpenClaw](https://github.com/openclaw/openclaw) 2026.3.11 | `examples/harness-configs/openclaw.json` | **Verified end-to-end** (macOS) — note the required `--session-id` |
| [Codex CLI](https://github.com/openai/codex) 0.143.0 | `examples/harness-configs/codex.json` | **Verified end-to-end** (macOS) |
| [Hermes Agent](https://github.com/nousresearch/hermes-agent) 0.18.1 | `examples/harness-configs/hermes.json` | **Verified end-to-end** (macOS, local Ollama qwen3:14b — a $0 wake path) |
| Generic webhook | `examples/harness-configs/generic-webhook.json` | Verified as a `curl` shape; supply your own URL |

Run the same proof against your own setup: `python scripts/verify_harness.py`.

Every config is a plain argv list; the literal substring `{briefing_file}`
is replaced with the path to a temp file holding that cycle's markdown
briefing before the command runs. Each config also carries `max_per_hour`
and `max_per_day` rate limits and can be disabled outright (`enabled:
false`). See `examples/harness-configs/README.md` for the full shape.

**Prior art & design positioning.** OpenClaw's community proposed a
"Thinking Clock" — a background tick loop with a cheap-LLM tier for idle
perception — in [issue #17287](https://github.com/openclaw/openclaw/issues/17287);
maintainers declined it for core on the reasonable grounds that it's heavy
orchestration, out of scope for a harness itself to own. ZugaMind is that
layer, built as a harness-agnostic sidecar instead of a fork of any one
harness's core, with one structural difference: its peripheral tier uses
no model at all. Idle perception is deterministic scanners plus salience
competition — free, and the first model call happens only after something
has already won the workspace and cleared the budget gate, not on every
tick.

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

Then try the always-on runner for one cycle, wired to the shipped Claude
Code harness config, in dry-run mode (no real subprocess call, no spend):

```bash
cp examples/harness-configs/claude-code.json zugamind/data/harness.json
python runner.py --once --dry-run
```

This sweeps the shipped scanners, runs one workspace cycle, and — if a
winner clears the action gate and it isn't currently quiet hours — journals
what *would* have woken Claude Code with the cycle's briefing
(`data/engine/journal.jsonl`), without ever invoking a subprocess. Drop
`--dry-run` (and set `ANTHROPIC_API_KEY`) to let it actually run; add
`--daemon [--interval 420]` to run forever instead of one cycle.

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
