#!/usr/bin/env python3
"""Why did condition A miss canaries? Trace them through the run journals."""
import json
import sys
from pathlib import Path

out = Path(sys.argv[1] if len(sys.argv) > 1 else "exp001-tier1")
s = json.load(open(out / "summary.json", encoding="utf-8"))

for x in s:
    if x["condition"] != "A":
        continue
    run = x["run"]
    missed = [k for k, v in x["per_canary"].items() if v["detected_tick"] is None]
    if not missed:
        continue
    print(f"=== A-run{run}: missed {missed} ===")
    journal = out / f"A-run{run}" / "engine" / "journal.jsonl"
    events = [json.loads(l) for l in open(journal, encoding="utf-8")]
    for cid in missed:
        planted = x["per_canary"][cid]["planted_tick"]
        print(f"  {cid} planted at tick {planted}:")
        cycle_i = 0
        for e in events:
            if e.get("kind") != "cycle":
                continue
            # canary re-emits 3 ticks; look at planted..planted+3 windows
            if planted <= cycle_i <= planted + 3:
                bids = e.get("bids", [])
                winner = e.get("winner") or {}
                in_bids = any(cid in json.dumps(b) for b in bids)
                in_winner = cid in json.dumps(winner)
                wsal = winner.get("salience")
                wmod = winner.get("source_module")
                # find the bid salience for the canary's module if present
                print(f"    tick {cycle_i}: canary_in_winner={in_winner} "
                      f"winner={wmod}@{wsal} bids={[(b['module'], b['salience']) for b in bids]}")
            cycle_i += 1
    print()
