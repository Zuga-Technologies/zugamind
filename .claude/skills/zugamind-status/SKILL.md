---
user-invocable: true
name: zugamind-status
description: Read-only lens into a running ZugaMind sidecar — current cognitive state, recent journal activity, budget spend, pause status, and wake/harness history. Use when the user asks "what has zugamind been doing", "is zugamind running", "show me the mind's history", "/zugamind-status", or wants visibility into a deployed sidecar without hand-reading its data files.
---

# zugamind-status

ZugaMind runs headless — it perceives, competes for attention, and wakes your harness
in the background, but nothing surfaces that history unless someone goes and reads its
data files by hand. This skill is that lens: a fast, read-only summary of what the mind
has actually been doing. It never writes to any ZugaMind file — status only.

## Step 1 — Locate the deployment

Default assumption: the current working directory (or a `zugamind/` subdirectory of it)
is a ZugaMind package root, matching the repo's own convention (`data/engine/` sits
directly under the package root the runner is invoked from).

Resolve in this order:
1. `$ZUGAMIND_DATA_DIR` env var, if set — data lives there directly.
2. `./data/engine/` relative to cwd.
3. `./zugamind/data/engine/` (cwd is a parent project embedding the package).
4. If none exist, search up to 2 directories deep for a `data/engine/state.json` and
   ask the user to confirm before proceeding — don't guess silently across an unrelated
   project.

The package root itself (parent of `data/`) is where the `PAUSE` kill-switch file would
live, per `foundation/config.py`.

## Step 2 — Read the state

From `<package_root>/data/engine/`:
- `state.json` — current cognitive state (RESTING/CURIOUS/FOCUSED/ALERT/REFLECTING),
  the reason for the last transition, and `last_wake` timestamp.
- `budget.json` — spend vs. `monthly_cap()` (default $10/mo unless
  `ZUGAMIND_MONTHLY_BUDGET_USD` overrides it).
- `<package_root>/PAUSE` — if this file exists, the mind is halted; say so prominently,
  everything below is "as of when it was paused."

## Step 3 — Summarize the journal

Tail `journal.jsonl` (JSONL, oldest first — read the last ~150-300 lines, no need for
the full file). Group by `kind` and report:

- **Cycle count & cadence** — how many `cycle` events, and roughly how far apart
  (gives a sense of whether it's actually alive vs. stalled).
- **Recent winners** — the last few `cycle` events that had a non-null `winner`, with
  `source_module` and a short excerpt of `content`.
- **Alarms** — any `alarm` events (salience >= 0.7) — these are the "something urgent
  fired" moments, surface them first if any exist in the window.
- **Harness wakes** — any `harness_skip` (why the gate refused) or actual invocations
  (from `_dispatch_to_harnesses` — inferred from `state["last_wake"]` advancing plus
  the absence of a `harness_skip`/`wake_filtered` on that cycle).
- **`wake_filtered`** — cycles where a winner existed but no configured harness wanted
  it (below its salience floor or wrong module) — worth noting if this fires a lot, it
  means the harness config's `wake_min_salience`/`wake_modules` may be too strict.
- **`work_claim`** events — flag any with `backed: false` prominently. This is
  ZugaMind's own post-hoc integrity check catching a harness reply that claimed to have
  done something with no matching git evidence — a real finding, not noise.
- **`paused`/`resumed`** — if these appear in the window, note when and for how long.

## Step 4 — Report

Plain, direct summary — not a wall of raw JSON. Shape:

```
State: FOCUSED (since <timestamp>, reason: <reason>)
Budget: $X.XX / $Y.YY this month
Cycles in window: N (last at <timestamp>)
Recent winners: ...
Alarms: ... (or "none")
Wakes: N harness invocation(s), M skipped (<reasons>)
Unbacked work claims: ... (or "none — all clean")
```

If the user asks a follow-up ("why did it wake at 3am", "what's this repo_issues thing
about"), read the specific journal entries around that timestamp for detail — the
summary above is the index, not the whole story.

## What this skill does NOT do

Read-only, always. It does not touch `PAUSE`, does not edit `state.json`/`budget.json`,
and does not invoke the runner. If the user wants to pause or resume, tell them the
manual lever (`touch PAUSE` / `rm PAUSE` at the package root) rather than doing it for
them unless they explicitly ask you to.
