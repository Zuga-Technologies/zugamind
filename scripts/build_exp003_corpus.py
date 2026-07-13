#!/usr/bin/env python3
"""Build the EXP-003 corpus (scripts/exp003_corpus.jsonl) — synthetic,
by design: a dominant noisy source and two planted ground-truth items,
built to create the starvation pressure EXP-001's corpus deliberately
avoided (EXP-001 diversified canaries across modules to STOP the diversity
cap firing; EXP-003 does the opposite on purpose).

Everything here is synthetic-by-construction, unlike EXP-001's real-HN
background — see exp-003-attention-health-ablation.md's threats-to-validity
section: this is a stress test, not a naturalistic sample, and any writeup
must say so.

Same JSONL row shape as scripts/exp001_corpus.jsonl:
    {"is_canary": false, "tick": N, "trigger": {...}}
    {"is_canary": true, "canary_id": "ZM-EXP003-...", "trigger": {...}}

Three item classes:
  - DOMINANT: infrastructure "degraded" chatter, one bid on nearly every
    tick, moderate salience (~0.4-0.7 via InfrastructureModule's own
    formula) — never critical urgency, so it never enters the alarm lane.
    It wins by sheer repetition, not by being individually alarming.
  - BURIED (the primary ground-truth item, ZM-EXP003-BURIED): one planted
    critical event from a DIFFERENT module (daemon), injected once at a
    fixed tick, competing against whatever the dominant source is doing
    that same tick.
  - DOMINANT-REAL-ALERT (ZM-EXP003-DOMREAL): one planted critical event
    from the SAME module as the dominant chatter (infrastructure), to test
    whether the health/alarm-lane machinery's suppression of that module's
    noise also accidentally suppresses its own genuine emergency.

Usage:
    python scripts/build_exp003_corpus.py --buried-tick 20 --domreal-tick 30
    python scripts/build_exp003_corpus.py --stats
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

CORPUS_FILE = Path(__file__).resolve().parent / "exp003_corpus.jsonl"
N_TICKS = 42  # same cadence as EXP-001/002 baseline: 4h/tick, simulated week


def make_dominant_events() -> list[dict]:
    """Infrastructure "degraded" chatter on EVERY tick, including the ticks
    the planted items land on — the whole point is head-to-head competition
    at the exact moment the buried signal appears, not a quiet corpus that
    happens to go silent right when it matters. (First draft of this script
    skipped the planted ticks — that accidentally removed the competitive
    pressure the experiment depends on; fixed same session.)"""
    out = []
    for tick in range(N_TICKS):
        out.append({
            "is_canary": False,
            "tick": tick,
            "trigger": {
                "type": "production_degraded",
                "detail": f"[dominant] Chatty monitor tick {tick}: elevated latency, "
                          f"non-critical, self-resolving",
                "novelty": 0.4,
                "relevance": 0.5,
                "urgency": 0.3,  # below ALARM_URGENCY (0.9) — never enters the lane
            },
        })
    return out


def make_buried_signal(tick: int) -> dict:
    """The primary ground-truth item: a genuine critical failure from a
    DIFFERENT module (daemon) than the dominant chatter (infrastructure).
    Marked critical-urgency so condition A's alarm lane should rescue it;
    condition D (bare lottery, no alarm lane) has to win it on raw salience
    against the dominant source's ongoing chatter."""
    cid = "ZM-EXP003-BURIED"
    return {
        "is_canary": True,
        "canary_id": cid,
        "tick": tick,
        "trigger": {
            "type": "daemon_task_failed",
            "detail": f"[{cid}] Background task queue deadlocked; no tasks completing. "
                      f"This item requires action; its id is {cid}.",
            "novelty": 1.0,
            "relevance": 1.0,
            "urgency": 0.95,
        },
    }


def make_dominant_real_alert(tick: int) -> dict:
    """H2 check: a genuine critical alert FROM the dominant source's own
    module (infrastructure), to test whether suppressing that module's
    noise also suppresses its real emergencies."""
    cid = "ZM-EXP003-DOMREAL"
    return {
        "is_canary": True,
        "canary_id": cid,
        "tick": tick,
        "trigger": {
            "type": "local_service_down",
            "detail": f"[{cid}] Monitored infrastructure service is fully down "
                      f"(not the usual degraded chatter). This item requires "
                      f"action; its id is {cid}.",
            "novelty": 1.0,
            "relevance": 1.0,
            "urgency": 0.95,
        },
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--buried-tick", type=int, default=20,
                        help="tick the buried signal (daemon) lands on")
    parser.add_argument("--domreal-tick", type=int, default=30,
                        help="tick the dominant-source real alert lands on")
    parser.add_argument("--stats", action="store_true", help="report only")
    args = parser.parse_args(argv)

    if args.stats:
        if not CORPUS_FILE.exists():
            print("no corpus built yet")
            return 0
        n_bg = n_canary = 0
        with open(CORPUS_FILE, encoding="utf-8") as fh:
            for line in fh:
                row = json.loads(line)
                if row.get("is_canary"):
                    n_canary += 1
                else:
                    n_bg += 1
        print(f"corpus: {n_bg} dominant-chatter events, {n_canary} planted items, "
              f"{N_TICKS} ticks")
        return 0

    if args.buried_tick == args.domreal_tick:
        raise SystemExit("--buried-tick and --domreal-tick must differ")

    rows: list[dict] = make_dominant_events()
    rows.append(make_buried_signal(args.buried_tick))
    rows.append(make_dominant_real_alert(args.domreal_tick))

    with open(CORPUS_FILE, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    n_dominant = sum(1 for r in rows if not r["is_canary"])
    print(f"built {CORPUS_FILE.name}: {n_dominant} dominant-chatter ticks, "
          f"buried signal @ tick {args.buried_tick}, "
          f"dominant-real-alert @ tick {args.domreal_tick}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
