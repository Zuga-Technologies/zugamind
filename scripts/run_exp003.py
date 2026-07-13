#!/usr/bin/env python3
"""EXP-003 runner: attention-health ablation (does selection-fairness
machinery earn its keep, or does a bare weighted lottery do just as well?).

Design: docs/experiments/exp-003-attention-health-ablation.md
Predictions: docs/experiments/exp-003-predictions.md

Two conditions, both running the REAL ZugaMind pipeline (unlike EXP-001's
B/C, there is no cron-dump condition here — this experiment isolates one
variable inside the workspace engine itself):

    A  full workspace — AttentionSchema soft corrections + alarm lane both
       active (Workspace(attention_health_enabled=True), the default).
    D  bare competition — both bypassed; winner is the raw salience^power
       weighted lottery every cycle (Workspace(attention_health_enabled=False)).

The critical-digest briefing mechanism is UNCHANGED in both conditions — it
is not a selection mechanism and is deliberately held constant (see the
design doc's motivation section for why).

Corpus is built fresh per run (not loaded from a frozen file) because the
pre-registered design commits to "varying which tick the buried signal and
the dominant real-alert land on" across repeats — each run derives its own
placement from --seed + run index, same spirit as EXP-001's seeded canary
placement.

Scoring: recall on ZM-EXP003-BURIED (primary, H1) and ZM-EXP003-DOMREAL
(H2), invocation count, false acts. Deterministic ACT: line matching, same
as EXP-001 — no LLM judges anywhere in the grading path.

Usage:
    python scripts/run_exp003.py --condition A --runs 5 --seed 20260711 --harness-config CFG
    python scripts/run_exp003.py --condition D --runs 5 --seed 20260711 --dry-run
    python scripts/run_exp003.py --smoke   # hermetic oracle run, both conditions, zero cost
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "zugamind"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from act import command_actuator  # noqa: E402
from continuity import journal  # noqa: E402
from foundation import config  # noqa: E402
from foundation import state as state_mod  # noqa: E402
from stream.runner import StreamRunner  # noqa: E402

from build_exp003_corpus import (  # noqa: E402
    N_TICKS,
    make_dominant_events,
    make_buried_signal,
    make_dominant_real_alert,
)
from run_exp001 import CorpusCursor, isolate_data_dir  # noqa: E402

SIM_START = datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)
CANARY_IDS = ("ZM-EXP003-BURIED", "ZM-EXP003-DOMREAL")


def canary_ids_in(text: str) -> set[str]:
    """EXP-003 uses two fixed, named IDs (not EXP-001's numeric-suffix
    scheme) — run_exp001's canary_ids_in is hardcoded to the ZM-EXP001-C##
    prefix and digit-scanning, so it silently matches nothing here. Literal
    substring check against the known pair instead."""
    return {cid for cid in CANARY_IDS if cid in text}


def build_run_corpus(seed: int) -> tuple[dict[int, list[dict]], dict[str, int]]:
    """Fresh corpus for one run: dominant chatter every tick, buried +
    dominant-real-alert at seed-derived, distinct ticks."""
    rng = random.Random(seed)
    buried_tick, domreal_tick = rng.sample(range(2, N_TICKS - 2), 2)

    events: dict[int, list[dict]] = {}
    for row in make_dominant_events():
        events.setdefault(row["tick"], []).append(row["trigger"])
    buried = make_buried_signal(buried_tick)
    domreal = make_dominant_real_alert(domreal_tick)
    events.setdefault(buried["tick"], []).append(buried["trigger"])
    events.setdefault(domreal["tick"], []).append(domreal["trigger"])

    planted = {"ZM-EXP003-BURIED": buried_tick, "ZM-EXP003-DOMREAL": domreal_tick}
    return events, planted


def run_condition(events: dict[int, list[dict]], attention_health_enabled: bool,
                  dry_run: bool) -> list[dict]:
    cursor = CorpusCursor(events)
    runner = StreamRunner(
        extra_scanners={"scan_corpus": cursor},
        dry_run=dry_run,
        include_default_scanners=False,
        attention_health_enabled=attention_health_enabled,
    )
    records = []
    for tick in range(N_TICKS):
        now = SIM_START + timedelta(hours=tick * 4)
        summary = runner.run_once(now=now)
        winner = summary.get("winner") or {}
        records.append({
            "tick": tick,
            "now": now.isoformat(),
            "trigger_count": summary.get("trigger_count", 0),
            "winner_module": winner.get("source_module"),
            "winner_salience": winner.get("salience"),
            "winner_canaries": sorted(canary_ids_in(str(winner.get("content", ""))
                                                    + str(winner.get("context", "")))),
            "invocations": len(summary.get("harness_results") or []),
            "harness_results": summary.get("harness_results") or [],
        })
    return records


def detected_ids(record: dict, dry_run: bool) -> set[str]:
    found: set[str] = set()
    for res in record.get("harness_results", []):
        stdout = res.get("stdout") or ""
        for line in stdout.splitlines():
            if line.strip().startswith("ACT:"):
                found |= {i for i in canary_ids_in(line) if i in CANARY_IDS}
    if dry_run and record.get("invocations"):
        found |= {i for i in record.get("winner_canaries", []) if i in CANARY_IDS}
    return found


def score(records: list[dict], planted: dict[str, int], dry_run: bool) -> dict:
    hits: dict[str, int] = {}
    false_acts = 0
    for rec in records:
        got = detected_ids(rec, dry_run)
        for cid in got:
            hits.setdefault(cid, rec["tick"])
        for res in rec.get("harness_results", []):
            for line in (res.get("stdout") or "").splitlines():
                s = line.strip()
                if s.startswith("ACT:") and not canary_ids_in(s):
                    false_acts += 1
    buried_hit = "ZM-EXP003-BURIED" in hits
    domreal_hit = "ZM-EXP003-DOMREAL" in hits
    return {
        "planted": len(planted),
        "detected": len(hits),
        "buried_recall": 1.0 if buried_hit else 0.0,
        "domreal_recall": 1.0 if domreal_hit else 0.0,
        "false_acts": false_acts,
        "total_invocations": sum(r.get("invocations", 0) for r in records),
        "per_canary": {c: {"planted_tick": planted[c], "detected_tick": hits.get(c)}
                       for c in sorted(planted)},
    }


def oracle_config(for_condition_full: bool) -> dict:
    cfg = {
        "name": "oracle",
        "command": [sys.executable, "-c",
                   "import sys,re;"
                   "t=open(sys.argv[1],encoding='utf-8').read();"
                   "ids=sorted(set(re.findall(r'ZM-EXP003-\\w+',t)));"
                   "[print('ACT: '+i) for i in ids];"
                   "print('NONE') if not ids else None",
                   "{briefing_file}"],
        "timeout_sec": 60,
        "max_per_hour": 100000,
        "max_per_day": 100000,
        "enabled": True,
    }
    if for_condition_full:
        cfg["wake_min_salience"] = 0.35
    return cfg


def run_once(condition: str, run_idx: int, seed: int, out_dir: Path,
            dry_run: bool, harness_cfg: dict | None) -> dict:
    events, planted = build_run_corpus(seed)
    run_dir = out_dir / f"{condition}-run{run_idx}"
    isolate_data_dir(run_dir)

    full = (condition == "A")
    cfg = dict(harness_cfg) if harness_cfg else oracle_config(for_condition_full=full)
    if "wake_min_salience" not in cfg:
        cfg["wake_min_salience"] = 0.35
    command_actuator.load_harness_configs = lambda *a, _cfg=cfg, **kw: [_cfg]
    # Same isolation as run_exp001: quiet hours come from the shared default
    # config file that a co-located live deployment may own — a simulated
    # week must never inherit the operator's sleep schedule (2026-07-12:
    # both planted canaries in A-run0 landed on quiet-hour sim-ticks and
    # were silently deferred).
    command_actuator.load_quiet_hours = lambda *a, **kw: None

    records = run_condition(events, attention_health_enabled=full, dry_run=dry_run)

    raw_path = out_dir / f"{condition}-run{run_idx}.jsonl"
    with open(raw_path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")

    metrics = score(records, planted, dry_run)
    metrics.update({"condition": condition, "run": run_idx, "seed": seed,
                    "dry_run": dry_run, "ticks": N_TICKS, "raw": str(raw_path.name)})
    return metrics


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--condition", choices=["A", "D"])
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20260711)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--harness-config", type=Path)
    parser.add_argument("--out", type=Path, default=Path("exp003-tier1"))
    parser.add_argument("--smoke", action="store_true",
                        help="hermetic oracle run, both conditions, zero cost")
    args = parser.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)

    if args.smoke:
        results = []
        for cond in ("A", "D"):
            # A non-dry A/D cycle hits the real wake-decision gate (Claude
            # API) before the oracle harness is even reached — smoke mode
            # must stay dry-run for both, same fix EXP-001's smoke uses.
            m = run_once(cond, 0, args.seed, args.out, dry_run=True, harness_cfg=None)
            print(f"[smoke] {cond}: buried_recall={m['buried_recall']} "
                  f"domreal_recall={m['domreal_recall']} inv={m['total_invocations']}")
            results.append(m)
        (args.out / "summary.json").write_text(json.dumps(results, indent=2))
        return 0

    if not args.condition:
        parser.error("--condition required unless --smoke")

    harness_cfg = None
    if args.harness_config:
        loaded = json.loads(args.harness_config.read_text())
        harness_cfg = loaded[0] if isinstance(loaded, list) else loaded
        harness_cfg = dict(harness_cfg)
        harness_cfg["enabled"] = True

    all_results = []
    summary_path = args.out / "summary.json"
    if summary_path.exists():
        all_results = json.loads(summary_path.read_text())

    for k in range(args.runs):
        m = run_once(args.condition, k, args.seed + k, args.out,
                     dry_run=args.dry_run, harness_cfg=harness_cfg)
        print(f"{args.condition}-run{k}: buried_recall={m['buried_recall']} "
              f"domreal_recall={m['domreal_recall']} "
              f"inv={m['total_invocations']} false_acts={m['false_acts']}")
        all_results = [r for r in all_results
                       if not (r["condition"] == args.condition and r["run"] == k)]
        all_results.append(m)

    summary_path.write_text(json.dumps(all_results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
