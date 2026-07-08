#!/usr/bin/env python3
"""ZugaMind minimal demo — a working GWT cycle with synthetic scanners.

Runs N workspace cycles against 3 toy scanner sources (no network, no API
key required) and prints, per cycle: every bid, the winner, and the
broadcast. If ANTHROPIC_API_KEY is set in the environment, the final
winner is optionally routed to Claude through the fail-closed action gate
(gates.action_gate) as a demonstration of the "deliberate work -> Claude"
path; otherwise it runs the same call in dry_run mode (no network, no spend).

Usage:
    python demo.py            # 8 cycles, dry-run action gate
    python demo.py --cycles 20
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "zugamind"))

from cognition.workspace import Workspace, create_all_modules, route_triggers_to_modules
from cognition.workspace.workspace_planner import WorkspacePlanner
from gates.action_gate import escalate_for_action

# --- Toy scanners -------------------------------------------------------
# Each returns a list of trigger dicts (see zugamind/scanners/_template.py
# for the real contract). These are synthetic — no network calls — so the
# demo runs anywhere with zero setup.


def scan_toy_infrastructure(rng: random.Random) -> list[dict]:
    if rng.random() < 0.25:
        return [{
            "type": "local_service_down",
            "service": "example-api",
            "port": 8000,
            "detail": "example-api not responding on :8000",
        }]
    return []


def scan_toy_code_changes(rng: random.Random) -> list[dict]:
    if rng.random() < 0.4:
        return [{
            "type": "git_commit",
            "project": "example-repo",
            "detail": rng.choice(["fix: null check in parser", "refactor: split module",
                                   "feat: add retry logic"]),
        }]
    return []


def scan_toy_external_signal(rng: random.Random) -> list[dict]:
    if rng.random() < 0.3:
        return [{
            "type": "hackernews_story",
            "detail": rng.choice([
                "Show HN: a stdlib-only agent workspace",
                "New paper on attention scheduling in autonomous agents",
            ]),
        }]
    return []


TOY_SCANNERS = [scan_toy_infrastructure, scan_toy_code_changes, scan_toy_external_signal]


def run_demo(cycles: int, seed: int) -> None:
    rng = random.Random(seed)

    workspace = Workspace()
    modules = create_all_modules()
    for m in modules:
        workspace.register_module(m)

    print(f"ZugaMind demo — {len(modules)} modules registered: "
          f"{[m.name for m in modules]}\n")

    planner = WorkspacePlanner()
    budget = {"remaining": 10.0}
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))

    for cycle in range(1, cycles + 1):
        triggers: list[dict] = []
        for scanner in TOY_SCANNERS:
            triggers.extend(scanner(rng))

        route_triggers_to_modules(triggers, modules)

        content = workspace.run_cycle({"cycle_number": cycle})

        print(f"--- cycle {cycle} " + "-" * 40)
        print(f"  triggers this cycle : {len(triggers)}")
        for bid in sorted(workspace.last_cycle_bids, key=lambda b: -b.salience):
            marker = " <- WINNER" if content and bid is content.bid else ""
            print(f"  bid  {bid.source_module:16s} salience={bid.salience:.3f}  "
                  f"{bid.content[:70]}{marker}")

        if content is None:
            print("  (no bids this cycle)")
            continue

        plan = planner.propose_plan(content, budget)
        print(f"  plan: {planner.format_plan_for_prompt(plan)}")

    # --- Optional: route the final winner to Claude through the action gate.
    if content is not None:
        intent = {
            "kind": "decide",
            "summary": f"Given the workspace winner '{content.content[:200]}', "
                       f"what is the single best next step?",
            "context": content.to_dict(),
            "caller": "demo.final_winner",
            "max_tokens": 200,
        }
        print("\n--- routing final winner through the action gate " + "-" * 10)
        result = escalate_for_action(intent, dry_run=not has_key)
        if has_key:
            print(f"  ok={result['ok']} model={result['model']} cost=${result['cost']:.4f}")
            if result.get("response"):
                print(f"  Claude says: {result['response'][:300]}")
        else:
            print(f"  (dry run — set ANTHROPIC_API_KEY to actually call Claude) -> {result}")

    print(f"\nDone. {cycles} cycles, {workspace.attention_schema.attention_switches} "
          f"attention switches, blind spots: {workspace.attention_schema.blind_spots}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=8, help="number of workspace cycles to run")
    parser.add_argument("--seed", type=int, default=7, help="RNG seed for the toy scanners")
    args = parser.parse_args()
    run_demo(args.cycles, args.seed)
