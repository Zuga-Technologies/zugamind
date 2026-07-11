#!/usr/bin/env python3
"""Aggregate EXP-001 N=5 results and score against pre-registered predictions."""
import json
import statistics as st
import sys
from pathlib import Path

out = Path(sys.argv[1] if len(sys.argv) > 1 else "exp001-tier1")
s = json.load(open(out / "summary.json", encoding="utf-8"))
by = {}
for r in s:
    by.setdefault(r["condition"], []).append(r)

print(f"{'cond':<6}{'runs':<6}{'recall':<9}{'range':<11}{'prec':<7}{'false':<7}{'wakes':<7}")
for c in "ABC":
    rs = by.get(c, [])
    if not rs:
        continue
    rec = [x["recall"] for x in rs]
    prec = [x["precision"] for x in rs]
    fa = sum(x["false_acts"] for x in rs)
    wk = [x["total_invocations"] for x in rs]
    print(f"{c:<6}{len(rs):<6}{st.mean(rec):<9.2f}{f'{min(rec):.1f}-{max(rec):.1f}':<11}"
          f"{st.mean(prec):<7.2f}{fa:<7}{st.mean(wk):<7.1f}")

print()
for c in "ABC":
    if c not in by:
        continue
    tt = [t for x in by[c] for t in x["time_to_detection_ticks"]]
    print(f"{c} time-to-detect: mean {st.mean(tt):.2f} ticks, max {max(tt)}")

print()
for c in ("B", "C"):
    tot = 0
    for i in range(5):
        f = out / f"{c}-run{i}.jsonl"
        if not f.exists():
            continue
        for line in open(f, encoding="utf-8"):
            tot += json.loads(line).get("context_chars", 0)
    print(f"{c} total prompt chars across 5 runs: {tot}")

# A prompt volume: briefings capped at 4000 chars per wake
a_wakes = sum(x["total_invocations"] for x in by.get("A", []))
print(f"A upper-bound prompt chars (wakes x 4KB cap): {a_wakes * 4000}")

# missed canaries per condition
print()
for c in "ABC":
    missed = {}
    for x in by.get(c, []):
        for k, v in x["per_canary"].items():
            if v["detected_tick"] is None:
                missed[k] = missed.get(k, 0) + 1
    print(f"{c} missed canaries (count over 5 runs): {missed or 'none'}")
