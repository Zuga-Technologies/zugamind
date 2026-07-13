#!/usr/bin/env python3
"""EXP-004 runner: shared arbitration (workspace) vs STEELMANNED per-source
threshold gates.

Design: docs/experiments/exp-004-strong-baseline-gates.md
Predictions (pre-registered): exp-004-predictions.md

Conditions:
    A  full workspace — the real pipeline via StreamRunner, exactly like
       EXP-003's condition A (post-EXP-001-fix: soft modulation + alarm lane
       + critical digest).
    E  per-source gates — one tuned threshold per configured source; an item
       above its source's threshold triggers a wake. Steelman requirements
       from the design doc are implemented literally:
         1. per-tick coalescing: all passing items in a tick share ONE wake;
         2. published tuning procedure (below);
         3. urgency override: items with urgency >= ALARM_URGENCY (0.9)
            always fire — the same guarantee A's alarm lane provides.

Tuning procedure for E (published, reproducible):
    Thresholds are calibrated on a CALIBRATION corpus generated with the
    same source plan but a DIFFERENT seed (seed+500), so E is tuned on the
    distribution, never on the measured corpus itself. Per source:
        threshold(source) = max(noise scores on calibration) + 0.05
    where score = (novelty + relevance + urgency) / 3 — the plain average a
    competent engineer would reach for first. Unknown sources (H4) get the
    DOCUMENTED DEFAULT: the median of the tuned thresholds.

Scoring: deterministic ACT-line matching on ZM-EXP004-C## ids, same as
EXP-001/003. Metrics: recall (overall + the H4 newcomer separately),
invocations, false acts, and the config-parameter count each condition
requires (workspace: 0 per-source parameters; gates: one per source).

Usage:
    python scripts/run_exp004.py --condition A --sources 4 --runs 3
    python scripts/run_exp004.py --condition E --sources 4 --runs 3
    python scripts/run_exp004.py --smoke     # hermetic, all cells, zero cost

NOTE on smoke numbers: in --smoke, condition A stays dry-run (a non-dry A
cycle hits the real wake-decision gate), and dry-run detection only counts
canaries visible in the WINNER slot — it is blind to canaries riding the
critical digest into briefings. Smoke A recall is therefore a FLOOR, not a
measurement (verified 2026-07-12: smoke A read 0.0 while the dry-run
journals showed the canary ids present in briefing content). Real runs
grade the actual briefing text via the oracle and do not have this gap.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "zugamind"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from act import command_actuator  # noqa: E402
from stream.runner import StreamRunner  # noqa: E402

from build_exp004_corpus import N_TICKS, build, source_plan, H4_SOURCE  # noqa: E402
from run_exp001 import CorpusCursor, isolate_data_dir  # noqa: E402

SIM_START = datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)
CANARY_PREFIX = "ZM-EXP004-C"
ALARM_URGENCY = 0.9  # mirror of the workspace alarm-lane threshold


def canary_ids_in(text: str) -> set[str]:
    ids, i = set(), 0
    while True:
        i = text.find(CANARY_PREFIX, i)
        if i == -1:
            return ids
        j = i + len(CANARY_PREFIX)
        while j < len(text) and text[j].isdigit():
            j += 1
        if j > i + len(CANARY_PREFIX):
            ids.add(text[i:j])
        i = j


def events_by_tick(rows: list[dict]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for row in rows:
        out.setdefault(row["tick"], []).append(row["trigger"])
    return out


# --------------------------------------------------------------------------
# condition E — the steelmanned gate baseline
# --------------------------------------------------------------------------

def item_score(trig: dict) -> float:
    return (float(trig.get("novelty", 0)) + float(trig.get("relevance", 0))
            + float(trig.get("urgency", 0))) / 3.0


def tune_thresholds(n_sources: int, seed: int) -> dict[str, float]:
    """The published tuning procedure (module docstring)."""
    cal_rows, _ = build(n_sources, seed + 500)
    per_source: dict[str, list[float]] = {}
    for row in cal_rows:
        if row["is_canary"]:
            continue  # tuned on NOISE only — incidents are not calibration data
        trig = row["trigger"]
        per_source.setdefault(trig["source_name"], []).append(item_score(trig))
    return {name: round(max(scores) + 0.05, 4)
            for name, scores in per_source.items() if scores}


def compose_gate_briefing(items: list[tuple[str, dict]]) -> str:
    lines = ["You are triaging observations for an autonomous system. For "
             "each item that reports a failure of THIS system's own monitored "
             "sources or services (such items describe a monitored feed/"
             "service failing and are marked as requiring action), output "
             "exactly one line 'ACT: <item-id>' using the item's id (ids "
             "look like ZM-EXP004-C##). Routine within-normal-band readings "
             "are not failures. After the ACT lines (if any), stop.", "",
             f"Items that cleared their source gate this tick ({len(items)}):"]
    for item_id, trig in items:
        lines.append(f"- id={item_id} source={trig.get('source_name')} "
                     f"type={trig.get('type')} :: {trig.get('detail', '')}")
    return "\n".join(lines)


def run_condition_e(events: dict[int, list[dict]], thresholds: dict[str, float],
                    dry_run: bool, harness_cfg: dict) -> list[dict]:
    default_thr = (statistics.median(thresholds.values())
                   if thresholds else 0.55)
    records, counter = [], 0
    for tick in range(N_TICKS):
        passing: list[tuple[str, dict]] = []
        for trig in events.get(tick, []):
            thr = thresholds.get(trig.get("source_name"), default_thr)
            fires = (item_score(trig) >= thr
                     or float(trig.get("urgency", 0)) >= ALARM_URGENCY)
            if fires:
                ids = canary_ids_in(str(trig))
                item_id = ids.pop() if ids else f"bg-{counter:04d}"
                counter += 1
                passing.append((item_id, trig))
        results = []
        if passing:  # steelman: ONE coalesced wake per tick, not per item
            briefing = compose_gate_briefing(passing)
            results = [command_actuator.invoke_harness(
                harness_cfg, briefing, dry_run=dry_run)]
        records.append({
            "tick": tick,
            "now": (SIM_START + timedelta(hours=tick * 4)).isoformat(),
            "gate_passing": sorted(i for i, _ in passing
                                   if i.startswith(CANARY_PREFIX)),
            "gate_items": len(passing),
            "invocations": len(results),
            "harness_results": results,
        })
    return records


# --------------------------------------------------------------------------
# condition A — the real pipeline (mirrors run_exp003)
# --------------------------------------------------------------------------

def run_condition_a(events: dict[int, list[dict]], dry_run: bool) -> list[dict]:
    cursor = CorpusCursor(events)
    runner = StreamRunner(extra_scanners={"scan_corpus": cursor},
                          dry_run=dry_run, include_default_scanners=False)
    records = []
    for tick in range(N_TICKS):
        summary = runner.run_once(now=SIM_START + timedelta(hours=tick * 4))
        winner = summary.get("winner") or {}
        records.append({
            "tick": tick,
            "now": (SIM_START + timedelta(hours=tick * 4)).isoformat(),
            "winner_module": winner.get("source_module"),
            "winner_salience": winner.get("salience"),
            "winner_canaries": sorted(canary_ids_in(
                str(winner.get("content", "")) + str(winner.get("context", "")))),
            "invocations": len(summary.get("harness_results") or []),
            "harness_results": summary.get("harness_results") or [],
        })
    return records


# --------------------------------------------------------------------------
# scoring — deterministic, shared by both conditions
# --------------------------------------------------------------------------

def score(records: list[dict], planted: dict[str, int], h4_cid: str,
          dry_run: bool, condition: str) -> dict:
    hits: dict[str, int] = {}
    false_acts = 0
    for rec in records:
        got: set[str] = set()
        for res in rec.get("harness_results", []):
            for line in (res.get("stdout") or "").splitlines():
                s = line.strip()
                if s.startswith("ACT:"):
                    ids = canary_ids_in(s)
                    got |= ids
                    if not ids:
                        false_acts += 1
        if dry_run and rec.get("invocations"):
            got |= set(rec.get("winner_canaries", []))
            got |= set(rec.get("gate_passing", []))
        for cid in got:
            if cid in planted:
                hits.setdefault(cid, rec["tick"])
            else:
                false_acts += 1
    recall = len(hits) / len(planted) if planted else 0.0
    return {
        "planted": len(planted),
        "detected": len(hits),
        "recall": round(recall, 4),
        "h4_detected": h4_cid in hits,
        "false_acts": false_acts,
        "total_invocations": sum(r.get("invocations", 0) for r in records),
        "per_canary": {c: {"planted_tick": planted[c], "detected_tick": hits.get(c)}
                       for c in sorted(planted)},
    }


def calibrate_workspace_floor(n_sources: int, seed: int, work_dir: Path) -> float:
    """EXP-004t: the workspace's ONE tuned parameter, calibrated by the same
    discipline as E's thresholds — on the calibration corpus (seed+500),
    never the measured one. Floor = max ambient (non-incident) WINNER
    salience observed in a model-free dry pass + 0.05. Safe for criticals:
    post-#11, alarm-lane winners bypass the wake floor entirely."""
    rows, _ = build(n_sources, seed + 500)
    events = events_by_tick(rows)
    isolate_data_dir(work_dir / "calib")
    records = run_condition_a(events, dry_run=True)
    ambient = [r["winner_salience"] for r in records
               if r.get("winner_salience") is not None
               and not r.get("winner_canaries")]
    return round((max(ambient) if ambient else 0.3) + 0.05, 4)


def oracle_config() -> dict:
    return {
        "name": "oracle",
        "command": [sys.executable, "-c",
                    "import sys,re;"
                    "t=open(sys.argv[1],encoding='utf-8').read();"
                    "ids=sorted(set(re.findall(r'ZM-EXP004-C\\d+',t)));"
                    "[print('ACT: '+i) for i in ids];"
                    "print('NONE') if not ids else None",
                    "{briefing_file}"],
        "timeout_sec": 60, "max_per_hour": 100000, "max_per_day": 100000,
        "enabled": True, "wake_min_salience": 0.35,
    }


def run_once(condition: str, n_sources: int, run_idx: int, seed: int,
             out_dir: Path, dry_run: bool) -> dict:
    rows, planted = build(n_sources, seed + run_idx)
    h4_cid = max(planted)  # H4's canary is always the highest-numbered id
    events = events_by_tick(rows)
    run_dir = out_dir / f"s{n_sources}-{condition}-run{run_idx}"
    isolate_data_dir(run_dir)

    cfg = oracle_config()
    command_actuator.load_harness_configs = lambda *a, _c=cfg, **kw: [_c]
    # Same isolation as run_exp001/003: a co-located deployment's quiet-hours
    # config must never leak into a simulated week (2026-07-12 incident).
    command_actuator.load_quiet_hours = lambda *a, **kw: None

    if condition == "A":
        records = run_condition_a(events, dry_run)
        n_params = 0
    elif condition == "At":
        # EXP-004t addendum: same workspace, one calibrated global floor
        # (see exp-004t-predictions.md). Calibration runs BEFORE the
        # measured corpus is touched, then the data dir is re-isolated.
        floor = calibrate_workspace_floor(n_sources, seed + run_idx, run_dir)
        cfg["wake_min_salience"] = floor
        command_actuator.load_harness_configs = lambda *a, _c=cfg, **kw: [_c]
        isolate_data_dir(run_dir)
        records = run_condition_a(events, dry_run)
        n_params = 1
    else:
        thresholds = tune_thresholds(n_sources, seed + run_idx)
        records = run_condition_e(events, thresholds, dry_run, cfg)
        n_params = len(thresholds)

    raw_path = out_dir / f"s{n_sources}-{condition}-run{run_idx}.jsonl"
    with open(raw_path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")

    metrics = score(records, planted, h4_cid, dry_run, condition)
    metrics.update({"condition": condition, "sources": n_sources,
                    "run": run_idx, "seed": seed + run_idx, "dry_run": dry_run,
                    "config_params": n_params, "raw": raw_path.name})
    return metrics


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--condition", choices=["A", "E", "At"])
    parser.add_argument("--sources", type=int, choices=[2, 4, 8, 12])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260712)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", type=Path, default=Path("exp004-out"))
    parser.add_argument("--smoke", action="store_true",
                        help="every (condition, sources) cell once, "
                             "hermetic oracle; A stays dry-run (real gate)")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    cells = ([(c, s) for s in (2, 4, 8, 12) for c in ("A", "E")]
             if args.smoke else [(args.condition, args.sources)])
    if not args.smoke and (not args.condition or not args.sources):
        parser.error("--condition and --sources required unless --smoke")
    runs = 1 if args.smoke else args.runs

    summary_path = args.out / "summary.json"
    all_results = (json.loads(summary_path.read_text())
                   if summary_path.exists() else [])
    for cond, n_sources in cells:
        for k in range(runs):
            dry = args.dry_run or (args.smoke and cond == "A")
            m = run_once(cond, n_sources, k, args.seed, args.out, dry)
            print(f"s{n_sources}-{cond}-run{k}: recall={m['recall']} "
                  f"h4={m['h4_detected']} inv={m['total_invocations']} "
                  f"false_acts={m['false_acts']} params={m['config_params']}")
            all_results = [r for r in all_results
                           if not (r["condition"] == cond and r["run"] == k
                                   and r["sources"] == n_sources)]
            all_results.append(m)
    summary_path.write_text(json.dumps(all_results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
