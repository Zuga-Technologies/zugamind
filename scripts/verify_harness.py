#!/usr/bin/env python3
"""End-to-end harness verification: prove ZugaMind can wake YOUR agent.

Injects a synthetic "canary" trigger carrying a unique token, boosts it so
it wins the workspace, and lets the full production dispatch path run:

    canary scanner -> workspace competition -> state machine -> briefing
    -> action gate -> command actuator -> YOUR harness subprocess

PASS means the harness's reply contains the canary token — i.e. your agent
was genuinely woken, read ZugaMind's briefing, and acted on it. This is the
same code path the daemon uses; nothing is mocked.

Usage:
    python scripts/verify_harness.py                # real invocation
    python scripts/verify_harness.py --dry-run      # no subprocess, no spend
    python scripts/verify_harness.py --max-cycles 8

Uses the same harness config as the runner (ZUGAMIND_HARNESS_CONFIG or
zugamind/data/harness.json). Each enabled harness is invoked at most once.
Exit code 0 = every enabled harness passed; 1 = any failure.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "zugamind"))

from act import command_actuator  # noqa: E402
from stream.runner import StreamRunner  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="journal what would run; no subprocess, no spend")
    parser.add_argument("--max-cycles", type=int, default=5,
                        help="cycles to attempt before giving up (selection is "
                             "weighted-random; the canary usually wins cycle 1)")
    args = parser.parse_args(argv)

    canary = f"ZM-CANARY-{int(time.time()) % 100000:05d}"

    def scan_canary():
        # Typed as a critical infrastructure trigger because that's the one
        # bid path that carries the trigger detail verbatim into the bid
        # content (and therefore into the briefing the harness reads).
        return [{
            "type": "local_service_down",
            "service": "zugamind-verify",
            "detail": (
                f"Harness verification canary {canary}. A human is testing that "
                f"ZugaMind can wake this harness. The woken agent should reply "
                f"with the token {canary} and one acknowledgement line, then stop."
            ),
            "novelty": 1.0,
            "relevance": 1.0,
            "urgency": 0.9,
        }]

    def boost_canary(bids, context):
        for b in bids:
            if canary in str(b.content):
                b.salience = 0.99
        return bids

    configs = [c for c in command_actuator.load_harness_configs() if c.get("enabled", True)]
    if not configs:
        print("no enabled harness configs found — set ZUGAMIND_HARNESS_CONFIG "
              "or create zugamind/data/harness.json (see examples/harness-configs/)")
        return 1

    print(f"canary token: {canary}")
    print(f"harnesses under test: {[c['name'] for c in configs]}")

    runner = StreamRunner(
        extra_scanners={"scan_canary": scan_canary},
        dry_run=args.dry_run,
        include_default_scanners=False,
    )
    runner.workspace.register_modulator(boost_canary)

    results = []
    for cycle in range(1, args.max_cycles + 1):
        summary = runner.run_once()
        winner = summary.get("winner") or {}
        won = canary in str(winner.get("content", ""))
        print(f"cycle {cycle}: winner={winner.get('source_module')} canary_won={won} "
              f"invocations={len(summary['harness_results'])}")
        if summary["harness_results"]:
            results = summary["harness_results"]
            break

    if not results:
        print(f"FAIL: no harness invocation in {args.max_cycles} cycles "
              "(check the gate/journal: zugamind/data/engine/journal.jsonl)")
        return 1

    all_ok = True
    print("\n--- results ---")
    for r in results:
        name = r.get("harness", "?")
        if args.dry_run:
            ok = bool(r.get("ok"))
            verdict = "PASS (dry-run: invocation composed, nothing executed)" if ok else "FAIL"
        else:
            echoed = canary in (r.get("stdout") or "")
            ok = bool(r.get("ok")) and r.get("returncode") == 0 and echoed
            verdict = ("PASS (harness woke, read the briefing, echoed the canary)"
                       if ok else
                       f"FAIL (ok={r.get('ok')} rc={r.get('returncode')} "
                       f"canary_echoed={echoed} err={str(r.get('error'))[:120]})")
        all_ok &= ok
        print(f"{name:14s} {verdict}")
        if not args.dry_run and r.get("stdout"):
            first_line = (r["stdout"] or "").strip().splitlines()
            print(f"{'':14s} reply: {first_line[0][:160] if first_line else ''}")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
