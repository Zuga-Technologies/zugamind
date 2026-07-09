<p align="center">
  <img src="docs/assets/zugamind-logo-512.png" width="180" alt="ZugaMind — one signal wins">
</p>

# ZugaMind

[![CI](https://github.com/Zuga-Technologies/zugamind/actions/workflows/ci.yml/badge.svg)](https://github.com/Zuga-Technologies/zugamind/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Dependencies: zero](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)](pyproject.toml)

**Agents answer. ZugaMind notices.**

![ZugaMind architecture — scanners feed a salience competition; one winner per cycle clears a fail-closed gate and wakes your harness](docs/assets/zugamind-hero.png)

An always-on attention sidecar for your agent harness — Claude Code, OpenClaw,
Codex CLI, Hermes, or any CLI process. It watches your sources for **free**
(no model calls while idle), runs a Global Workspace salience competition
over what it sees, and wakes your harness with a continuity briefing only
when something wins the competition **and** clears a fail-closed budget gate.
Python stdlib only. Zero dependencies.

Built by Zuga Technologies. Independent work — not affiliated with or
endorsed by Anthropic.

## Install

**Requirements: Python 3.10+ and git. Nothing else** — zero dependencies
means there is no install step:

```bash
git clone https://github.com/Zuga-Technologies/zugamind.git
cd zugamind
python demo.py        # offline demo — no key, no network
```

![demo.py — cycles of salience competition, one winner per cycle, winner routed through the action gate in dry-run](docs/assets/zugamind-demo.gif)

Optional, only for the test tooling:

```bash
pip install -e ".[dev]"   # adds pytest, nothing else
pytest -q                 # 262 tests — no network, no keys, ~4s
```

Linux, macOS, Windows all work (CI runs all three × Python 3.10–3.13).

## Set up the sidecar — 3 steps

**1. Copy the config for your harness:**

```bash
cp examples/harness-configs/claude-code.json zugamind/data/harness.json
```

(`openclaw.json`, `codex.json`, `hermes.json`, `generic-webhook.json` also ship.)

**2. Open `zugamind/data/harness.json` and set `"enabled": true`.**
Every shipped config is disabled — going live is always an explicit act,
never a side effect of copying a file. Before unattended runs, also set
`wake_modules` / `wake_min_salience` (see `examples/harness-configs/README.md`)
so the harness only wakes for sources you care about.

**3. Dry-run once, then start the daemon:**

```bash
python runner.py --once --dry-run   # one full cycle; journals the would-be wake, no spend
python runner.py --daemon           # always-on (set ANTHROPIC_API_KEY for paid tiers)
```

Unlike `demo.py`, `runner.py` makes real read-only HTTP requests (the
shipped scanners poll the HackerNews API and public RSS feeds). `--dry-run`
means "no harness subprocess, no model spend" — perception itself is live.
Everything lands in `zugamind/data/engine/journal.jsonl`.

**Prove the wake path against your own install:**

```bash
python scripts/verify_harness.py
```

This plants a canary trigger, lets it win the workspace, clears the gate,
spawns your real harness, and checks the agent's reply echoes the canary —
the same code path the daemon uses, nothing mocked.

| Harness | Config | Status |
|---|---|---|
| [Claude Code](https://claude.com/claude-code) 2.1.204 | `examples/harness-configs/claude-code.json` | **Verified end-to-end** (Windows) |
| [OpenClaw](https://github.com/openclaw/openclaw) 2026.3.11 | `examples/harness-configs/openclaw.json` | **Verified end-to-end** (macOS) — note the required `--session-id` |
| [Codex CLI](https://github.com/openai/codex) 0.143.0 | `examples/harness-configs/codex.json` | **Verified end-to-end** (macOS) |
| [Hermes Agent](https://github.com/nousresearch/hermes-agent) 0.18.1 | `examples/harness-configs/hermes.json` | **Verified end-to-end** (macOS, local Ollama qwen3:14b — a $0 wake path) |
| Generic webhook | `examples/harness-configs/generic-webhook.json` | Verified as a `curl` shape; supply your own URL |

Configs are plain argv lists; `{briefing_file}` is replaced with the path to
that cycle's markdown briefing. Each config carries `max_per_hour` and
`max_per_day` rate limits.

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

Every arrow is inspectable: `Workspace.get_stats()` after any cycle returns
every bid that competed, the winner, the runner-up, and the attention
schema's self-model (recent foci, blind spots, stuck-detection,
attention-switch count). Extend admission with a plain Python callback via
`Workspace.register_modulator()`.

Three pieces close the loop from "decided" to "your harness is working on it":

- **`continuity/journal.py`** — append-only episodic log of every notable
  event. `build_briefing()` turns its tail into the markdown a waking
  harness reads: why it's being woken, what happened since the last wake,
  what's unresolved. Hard-capped (~4000 chars) — orientation, not noise.
- **`act/command_actuator.py`** — the harness adapter: writes the briefing
  to a temp file, substitutes `{briefing_file}` into your configured argv,
  runs it as a subprocess. Rate-limited per rolling hour AND day (counted
  from the durable journal — an unreadable journal refuses the wake rather
  than resetting the count), never raises, honors quiet hours
  (`ZUGAMIND_QUIET_HOURS`).
- **`stream/runner.py`** — the always-on loop. Perception and journaling
  never stop; quiet-hours winners are deferred (journaled, not lost) and
  surface in the next real briefing. `touch PAUSE` at the package root
  halts the whole cycle; `rm PAUSE` resumes.

Your harness's own code never changes — ZugaMind attaches as a sidecar
process and taps it on the shoulder with full context.

## Why not just cron?

| | cron / heartbeat | ZugaMind |
|---|---|---|
| Idle cost | A model call per tick, mattered or not | $0 — deterministic scanners + salience math; the first token billed is after something already won the competition |
| Trigger | The clock | Salience — something changed *and* out-competed everything else this cycle |
| Repeats | Re-alerts on the same thing forever | Habituation — a seen trigger is damped for hours (`ZUGAMIND_HABITUATION_HOURS`) |
| Attention health | N/A | Streak dampening, diversity caps, blind-spot boosts — no source can monopolize wakes |
| Context on wake | Whatever your script passes | A capped continuity briefing: why you're being woken, what happened since last wake, what's unresolved |
| Runaway protection | You write it | Fail-closed gate + hard $ cap + per-hour/per-day invocation caps counted from a durable journal |

If you'd rather read the code as "a priority queue with decay and rate
limits", it works identically under that description — the GWT vocabulary
is the design lineage, not a load-bearing claim.

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
  *spend*, never *thinking*. If persisting a spend to the ledger fails
  even after a retry, the gate keeps the already-paid-for response but
  returns `budget_persisted: False` and logs at ERROR — callers and
  monitoring should treat that as "the cap is temporarily unenforceable",
  never ignore it.
- **Everything ships disabled or dry-run.** Every example harness config
  ships `"enabled": false`; the runner has `--dry-run`; wiring a live wake
  path is always an explicit human act.
- **Know the prompt-injection surface.** The scanners ingest text from the
  open internet (GitHub issue titles, HN/Reddit post titles — anyone can
  write those), and that text flows into the briefing your harness is asked
  to act on. ZugaMind's content screen blocks the clear-cut attack phrasings
  it knows about, but the real defense is the harness's own permission
  model: run wakes through your harness's normal approval prompts (the
  shipped Claude Code config deliberately does NOT pass
  `--dangerously-skip-permissions`), scope `wake_modules` to sources you
  trust, and treat briefing content as untrusted input, because upstream,
  it is.
- **A human veto point.** Any intent can be marked `requires_human: True`;
  the gate refuses to execute it at all. Wiring an actual notification
  (Discord, Slack, email, a ticket) onto that refusal is left to the
  integrator — the core guarantees the refusal, not the paging.
- **Post-hoc integrity checks.** Two are wired into the shipped loop; two
  ship as opt-in library modules for deployments that have the matching
  surface:
  - `gates/work_claim.py` — **wired**: every real (non-dry-run) harness
    reply is checked for accomplishment claims against real git history; a
    claim with no matching commit is journaled as a `work_claim` event
    flagged as confabulation, regardless of how confidently it's phrased.
  - `gates/value_gate.py` — **wired, ships dark**: registered as a bid
    modulator that dampens the salience of bid types that historically
    didn't change real state, plus a post-wake scorer that feeds it. A
    byte-identical no-op until you opt in via
    `ZUGAMIND_VALUE_GATE_ENABLED=true`.
  - `gates/operational_truth.py` — **opt-in library**: a freshness gate
    that re-probes live state before a claim is allowed to surface, so a
    true-once observation can't be re-narrated as still-true indefinitely.
    Populate its service-port map with your deployment's services and
    inject `format_block()` into your briefing/prompt path.
  - `gates/self_mod_cooldown.py` — **opt-in library**: a restart-durable,
    disk-backed cooldown so a self-modification proposal can't thrash the
    same file repeatedly — for integrators whose harness has a
    self-modification lane.
- **Fully logged.** `Workspace.get_stats()`, the attention schema's
  `get_context()`, and every gate's telemetry are structured and
  loggable every cycle — "why did it do that" should always be answerable
  from the log, not from re-running the model.

## Why now

On 2026-07-06, Anthropic published ["A global workspace in language
models"](https://transformer-circuits.pub/2026/workspace): evidence that
Claude's own internal computation exhibits a global-workspace-like structure
— a limited-capacity bottleneck, broadcast, competition for access. The
paper is explicit that **what decides admission is still unknown** — the
mechanism is emergent, discovered after the fact.

ZugaMind is the engineered complement at a different level of the stack: the
same pattern — one bottleneck, one winner per cycle, broadcast, competition —
built as an external, steerable substrate that decides when your LLM gets
invoked at all. The one open question the paper names is the one thing
ZugaMind answers explicitly.

| Paper's GWT observation | ZugaMind's mechanism |
|---|---|
| Limited capacity — one winner per cycle | `Workspace.run_cycle()` selects exactly one `SalienceBid` per cycle |
| Broadcast to the rest of the network | `WorkspaceModule.on_broadcast()` fires on every registered module every cycle |
| Competition for access | Modules submit `SalienceBid`s; `AttentionSchema.modulate()` re-weights them before selection |
| Admission mechanism unknown / not steerable | `AttentionSchema` (streak dampening, diversity cap, blind-spot boost, novelty bonus) + pluggable `register_modulator()` hooks — explicit and inspectable |
| Reportability | `Workspace.get_stats()` returns the full bid field, the winner, and the attention self-model every single cycle |
| Deliberate vs. automatic processing | Cheap/free local-model tier for routine cycles; the fail-closed action gate escalates to Claude only when a winner's work justifies the spend |

## Related work & prior art

- OpenClaw's community proposed a "Thinking Clock" — a background tick loop
  with a cheap-LLM tier — in [issue #17287](https://github.com/openclaw/openclaw/issues/17287),
  closed as a duplicate of the broader
  ["Thinking Agents Manifesto" (#17363)](https://github.com/openclaw/openclaw/issues/17363),
  which maintainers closed as not planned for core (VISION.md avoids
  "shipping heavy orchestration layers as a default architecture in core").
  ZugaMind is that external layer, harness-agnostic, with one structural
  difference: the idle tier uses **no model at all**. The manifesto author's
  own prototype is [amor71/thinking-agents](https://github.com/amor71/thinking-agents).
- [Anthropic, "A global workspace in language models"](https://transformer-circuits.pub/2026/workspace)
  (2026-07-06) — see [Why now](#why-now); this repo makes no claim about
  model internals.
- [giansha/Global-Workspace-Agents](https://github.com/giansha/Global-Workspace-Agents)
  ([arXiv 2604.08206](https://arxiv.org/abs/2604.08206)) — academic GWT
  multi-agent framework; research-oriented rather than a harness sidecar.
- [bwcummings1/limen](https://github.com/bwcummings1/limen) — a stdlib GWT
  runtime demo. Different gap: LIMEN demonstrates the loop; ZugaMind ships
  the loop with verified harness adapters, budget/rate-limit safety, and a
  continuity journal for unattended operation.

## Limitations

- This is a **macro-scale task workspace** — an external orchestration
  layer that decides what an agent attends to and when it escalates to an
  LLM. It is **not** a claim about, model of, or replacement for the
  internal, emergent workspace Anthropic's interpretability research
  describes inside the model's own computation. The paper mapping above is
  an analogy at the pattern level, not an implementation of the same
  mechanism.
- The example modules in `cognition/workspace/workspace_modules.py` and the
  four world-scanners in `scanners/world/` are illustrative, not a
  production perception stack. Replace them with your own. The Reddit
  scanner in particular rides unauthenticated public RSS — best-effort by
  design; it may be rate-limited or blocked without notice and fails silent
  to an empty list.
- The budget model (`foundation/budget.py`) is a simple standalone monthly
  cap by design — it is not a multi-agent fleet-wide accounting system.
  Integrators running several agents against a shared budget should supply
  their own `monthly_cap()`.
- `gates/action_gate.py`'s content screen is a regex-based acute safety net,
  not a general-purpose alignment solution — it catches clear-cut,
  named failure classes, not everything that could go wrong. It is one
  layer; your harness's permission model is the load-bearing one (see the
  prompt-injection note under Safety design).
- `gates/work_claim.py`'s entity-grounding check is weaker on Windows: it
  uses the POSIX system dictionary (`/usr/share/dict/words`) to filter
  ordinary capitalized words, which doesn't exist on Windows, so only the
  curated stoplist applies there. Documented in the module; fail-open
  either way.

## License

Apache 2.0 — see `LICENSE` and `NOTICE`. The Apache License does not grant
trademark rights (§6): "ZugaMind" and "Zuga Technologies" are trade names of
Zuga Technologies LLC — fork the code freely, but ship your fork under your
own name.
