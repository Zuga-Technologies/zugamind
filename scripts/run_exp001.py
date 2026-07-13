#!/usr/bin/env python3
"""EXP-001 runner: external salience selection vs. in-context triage.

Design, hypotheses, and pre-registration protocol:
    docs/experiments/exp-001-external-workspace-vs-in-context-triage.md

Three conditions over the same frozen corpus (scripts/exp001_corpus.jsonl):

    A  external workspace — events replay through the REAL ZugaMind pipeline
       (workspace competition -> gate -> actuator). The model sees one
       briefing per wake. Nothing is mocked; this is verify_harness.py's
       injection seam driven by a corpus cursor instead of a single canary.
    B  cron + dump — every tick, the harness is invoked directly with all
       events accumulated since the last tick. No workspace, no gate
       selection: the model is its own triage.
    C  cron + ask — same accumulation as B, but the prompt explicitly asks
       whether anything needs action.

Ground truth: planted canary items carry unique IDs (ZM-EXP001-Cnn). The
task instruction in every condition asks the agent to emit one line
`ACT: <canary-id>` per item that requires action. Scoring is deterministic
string matching — no LLM judges anywhere in the grading path.

Usage:
    python scripts/run_exp001.py --condition A --runs 5 --harness-config CFG
    python scripts/run_exp001.py --condition B --runs 5 --dry-run   # mechanics only
    python scripts/run_exp001.py --smoke                            # hermetic oracle run

--dry-run exercises composition/journaling with no subprocess and no spend
(precision/recall need a real or oracle harness). --smoke runs all three
conditions once against a deterministic oracle harness (a python one-liner
that echoes ACT lines for every canary ID present in its input) — proves
the full measurement path end to end with zero network and zero cost.

Per-run isolation: each repeat gets a fresh data dir (journal, state,
habituation store) so rate limits and habituation never leak across runs.
Every run writes raw per-tick records to <out>/<cond>-run<k>.jsonl and a
metrics block to <out>/summary.json. Seeds are recorded; runs replay.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "zugamind"))

from act import command_actuator  # noqa: E402
from continuity import journal  # noqa: E402
from foundation import config  # noqa: E402
from foundation import state as state_mod  # noqa: E402
from stream.runner import StreamRunner  # noqa: E402

CORPUS_FILE = Path(__file__).resolve().parent / "exp001_corpus.jsonl"
CANARY_PREFIX = "ZM-EXP001-C"
SIM_START = datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)
N_TICKS = 42  # simulated week at 4h/tick; must match build_exp001_corpus.py
NATIVE_TICK_HOURS = 4.0  # the corpus's own time resolution (EXP-002 design
                         # doc, threats: the very-fast cadence cannot resolve
                         # events finer than this grid)

TASK_INSTRUCTION = (
    "You are triaging observations for an autonomous system. For each item "
    "that reports a failure of THIS system's own monitored sources or "
    "services (such items describe a monitored feed/service failing and are "
    "marked as requiring action), output exactly one line 'ACT: <item-id>' "
    "using the item's id. News or discussion ABOUT third-party outages is "
    "not a monitored-source failure. After the ACT lines (if any), stop."
)


# --------------------------------------------------------------------------
# corpus
# --------------------------------------------------------------------------

def load_corpus(path: Path) -> tuple[dict[int, list[dict]], list[dict], int]:
    """Return (background events by tick, canary definitions, tick count)."""
    background: dict[int, list[dict]] = {}
    canaries: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            if row.get("is_canary"):
                canaries.append(row)
                continue
            background.setdefault(int(row["tick"]), []).append(row["trigger"])
    return background, canaries, N_TICKS


CANARY_PERSIST_TICKS = 3  # a real failed-source alarm re-fires each scan
                          # until fixed; canaries re-emit for this many ticks


def place_canaries(canaries: list[dict], n_ticks: int, seed: int) -> dict[int, list[dict]]:
    """Assign each canary an onset tick for this run — seeded, replayable.

    Each canary re-emits for CANARY_PERSIST_TICKS consecutive ticks from its
    onset, mirroring how a genuine monitored-source failure keeps alarming
    until repaired. Time-to-detection is measured from the ONSET tick.
    """
    rng = random.Random(seed)
    placed: dict[int, list[dict]] = {}
    # avoid tick 0 (no 'since last wake' context yet) and leave room at the
    # end so persistence + a slow detection still fit inside the window.
    last_onset = max(2, n_ticks - CANARY_PERSIST_TICKS)
    ticks = rng.sample(range(1, last_onset), k=min(len(canaries), last_onset - 1))
    for canary, onset in zip(canaries, ticks):
        for t in range(onset, min(onset + CANARY_PERSIST_TICKS, n_ticks)):
            placed.setdefault(t, []).append(canary["trigger"])
    return placed


class CorpusCursor:
    """A scan_* callable that yields each simulated tick's event batch."""

    def __init__(self, events_by_tick: dict[int, list[dict]]):
        self._by_tick = events_by_tick
        self.tick = 0

    def __call__(self) -> list[dict]:
        batch = self._by_tick.get(self.tick, [])
        self.tick += 1
        return batch


def merge_ticks(*sources: dict[int, list[dict]]) -> dict[int, list[dict]]:
    merged: dict[int, list[dict]] = {}
    for src in sources:
        for tick, batch in src.items():
            merged.setdefault(tick, []).extend(batch)
    return merged


def native_to_run_tick(native_tick: int, tick_hours: float) -> int:
    """Map a native-grid tick (4h resolution) to the run grid's tick index."""
    return int(native_tick * NATIVE_TICK_HOURS // tick_hours)


def rebucket_ticks(events: dict[int, list[dict]], tick_hours: float
                   ) -> tuple[dict[int, list[dict]], int]:
    """Replay the SAME simulated timeline on a different polling grid
    (EXP-002 cadence sweep). Events keep their simulated timestamps —
    an event at native tick T (sim-time T*4h) lands in whichever run-grid
    tick contains that moment. At tick_hours=4.0 this is the identity
    (EXP-001 behavior unchanged); coarser grids merge native ticks,
    finer grids leave most run ticks empty — by design, since the corpus
    is only resolved to NATIVE_TICK_HOURS.
    """
    out: dict[int, list[dict]] = {}
    for tick, batch in events.items():
        out.setdefault(native_to_run_tick(tick, tick_hours), []).extend(batch)
    n_run_ticks = int(-(-(N_TICKS * NATIVE_TICK_HOURS) // tick_hours))  # ceil
    return out, n_run_ticks


def canary_ids_in(text: str) -> set[str]:
    ids = set()
    i = 0
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


# --------------------------------------------------------------------------
# per-run isolation
# --------------------------------------------------------------------------

def isolate_data_dir(run_dir: Path) -> None:
    """Point journal/state/habituation at a fresh dir (test-suite pattern)."""
    engine = run_dir / "engine"
    engine.mkdir(parents=True, exist_ok=True)
    journal.JOURNAL_FILE = engine / "journal.jsonl"
    state_mod.STATE_FILE = engine / "state.json"
    state_mod.ENGINE_DIR = engine
    config.SEEN_TRIGGERS_FILE = engine / "seen_triggers.json"


# --------------------------------------------------------------------------
# conditions
# --------------------------------------------------------------------------

def run_condition_a(events: dict[int, list[dict]], n_ticks: int, tick_hours: float,
                    dry_run: bool) -> list[dict]:
    """The real pipeline: competition decides, gate + actuator dispatch."""
    cursor = CorpusCursor(events)
    runner = StreamRunner(
        extra_scanners={"scan_corpus": cursor},
        dry_run=dry_run,
        include_default_scanners=False,
    )
    records = []
    for tick in range(n_ticks):
        now = SIM_START + timedelta(hours=tick * tick_hours)
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


def compose_dump(items: list[tuple[str, dict]], ask: bool) -> str:
    lines = [TASK_INSTRUCTION, ""]
    if ask:
        lines.append("Question: does anything below need action? If nothing "
                     "does, output exactly 'NONE'.")
        lines.append("")
    lines.append(f"Observations since last check ({len(items)} items):")
    for item_id, trig in items:
        lines.append(f"- id={item_id} type={trig.get('type')} :: {trig.get('detail', '')}")
    return "\n".join(lines)


def run_condition_bc(events: dict[int, list[dict]], n_ticks: int, tick_hours: float,
                     dry_run: bool, ask: bool, harness_cfg: dict) -> list[dict]:
    """Cron conditions: no workspace, no selection — the model triages."""
    records = []
    counter = 0
    for tick in range(n_ticks):
        now = SIM_START + timedelta(hours=tick * tick_hours)
        batch = []
        for trig in events.get(tick, []):
            ids = canary_ids_in(str(trig))
            item_id = ids.pop() if ids else f"bg-{counter:04d}"
            counter += 1
            batch.append((item_id, trig))
        prompt = compose_dump(batch, ask=ask)
        result = command_actuator.invoke_harness(harness_cfg, prompt, dry_run=dry_run)
        records.append({
            "tick": tick,
            "now": now.isoformat(),
            "context_items": len(batch),
            "context_chars": len(prompt),
            "canaries_in_context": sorted(i for i, _ in batch if i.startswith(CANARY_PREFIX)),
            "invocations": 1,
            "harness_results": [result],
        })
    return records


# --------------------------------------------------------------------------
# scoring — deterministic, no LLM anywhere
# --------------------------------------------------------------------------

def detected_ids(record: dict, condition: str, dry_run: bool) -> set[str]:
    """Canaries this tick's record counts as ACTED ON."""
    found: set[str] = set()
    for res in record.get("harness_results", []):
        stdout = res.get("stdout") or ""
        for line in stdout.splitlines():
            if line.strip().startswith("ACT:"):
                found |= canary_ids_in(line)
    if condition == "A" and dry_run:
        # dry-run composes but never executes: count a canary WAKE (the
        # pipeline selected and dispatched it) as detection-of-mechanics.
        if record.get("invocations"):
            found |= set(record.get("winner_canaries", []))
    return found


def score(records: list[dict], planted: dict[str, int], condition: str,
          dry_run: bool) -> dict:
    hits: dict[str, int] = {}
    false_acts = 0
    total_act_lines = 0
    for rec in records:
        got = detected_ids(rec, condition, dry_run)
        total_act_lines += len(got)
        for cid in got:
            if cid in planted:
                hits.setdefault(cid, rec["tick"])
            else:
                false_acts += 1
        for res in rec.get("harness_results", []):
            stdout = res.get("stdout") or ""
            for line in stdout.splitlines():
                s = line.strip()
                if s.startswith("ACT:") and not canary_ids_in(s):
                    false_acts += 1
    recall = len(hits) / len(planted) if planted else 0.0
    precision = (len(hits) / (len(hits) + false_acts)) if (hits or false_acts) else 1.0
    ttd = [hits[c] - planted[c] for c in hits]
    return {
        "planted": len(planted),
        "detected": len(hits),
        "recall": round(recall, 4),
        "precision": round(precision, 4),
        "false_acts": false_acts,
        "time_to_detection_ticks": sorted(ttd),
        "total_invocations": sum(r.get("invocations", 0) for r in records),
        "per_canary": {c: {"planted_tick": planted[c], "detected_tick": hits.get(c)}
                       for c in sorted(planted)},
    }


# --------------------------------------------------------------------------
# oracle harness (hermetic smoke)
# --------------------------------------------------------------------------

ORACLE_SRC = (
    "import sys,re;"
    "t=open(sys.argv[1],encoding='utf-8').read();"
    "ids=sorted(set(re.findall(r'ZM-EXP001-C\\d+',t)));"
    "[print('ACT: '+i) for i in ids];"
    "print('NONE') if not ids else None"
)


def oracle_config(name: str = "oracle", for_condition_a: bool = False) -> dict:
    cfg = {
        "name": name,
        "command": [sys.executable, "-c", ORACLE_SRC, "{briefing_file}"],
        "timeout_sec": 60,
        "max_per_hour": 100000,
        "max_per_day": 100000,
        "enabled": True,
    }
    if for_condition_a:
        # The product's selectivity lever, pre-declared for the experiment:
        # condition A only wakes the harness for winners above this bar.
        cfg["wake_min_salience"] = 0.35
    return cfg


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def run_once_for_condition(condition: str, run_idx: int, seed: int, out_dir: Path,
                           dry_run: bool, tick_hours: float,
                           harness_cfg: dict | None) -> dict:
    background, canaries, n_native_ticks = load_corpus(CORPUS_FILE)
    # Canaries are placed on the NATIVE grid regardless of run cadence, so
    # one seed produces one simulated timeline that every cadence replays —
    # the cadence sweep varies only how finely that timeline is polled.
    placed = place_canaries(canaries, n_native_ticks, seed)
    planted: dict[str, int] = {}
    for tick, batch in placed.items():
        for trig in batch:
            for cid in canary_ids_in(str(trig)):
                # onset tick = earliest emission (persistence re-emits later)
                planted[cid] = min(planted.get(cid, tick), tick)
    events, n_ticks = rebucket_ticks(merge_ticks(background, placed), tick_hours)
    planted = {cid: native_to_run_tick(t, tick_hours) for cid, t in planted.items()}

    run_dir = out_dir / f"{condition}-run{run_idx}"
    isolate_data_dir(run_dir)

    if condition == "A":
        cfg = dict(harness_cfg) if harness_cfg else oracle_config(for_condition_a=True)
        if "wake_min_salience" not in cfg:
            cfg["wake_min_salience"] = 0.35
        command_actuator.load_harness_configs = (  # experiment config wins
            lambda *a, _cfg=cfg, **kw: [_cfg])
        # Isolate quiet hours too: load_quiet_hours() reads the DEFAULT
        # data-dir config file, which a co-located live deployment may own.
        # Caught 2026-07-12: a dogfood daemon's harness.json (quiet 23:00-
        # 08:00) silently deferred every simulated wake whose sim-time fell
        # in that window — contaminating 4 sweep runs. Simulated weeks have
        # no operator sleep schedule.
        command_actuator.load_quiet_hours = lambda *a, **kw: None
        records = run_condition_a(events, n_ticks, tick_hours, dry_run)
    else:
        cfg = harness_cfg or oracle_config()
        records = run_condition_bc(events, n_ticks, tick_hours, dry_run,
                                   ask=(condition == "C"), harness_cfg=cfg)

    raw_path = out_dir / f"{condition}-run{run_idx}.jsonl"
    with open(raw_path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")

    metrics = score(records, planted, condition, dry_run)
    metrics["time_to_detection_hours"] = [
        round(t * tick_hours, 2) for t in metrics["time_to_detection_ticks"]
    ]
    metrics.update({"condition": condition, "run": run_idx, "seed": seed,
                    "dry_run": dry_run, "ticks": n_ticks,
                    "tick_hours": tick_hours, "raw": str(raw_path)})
    return metrics


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--condition", choices=["A", "B", "C"])
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20260713)
    parser.add_argument("--tick-hours", type=float, default=4.0,
                        help="polling-grid interval in simulated hours "
                             "(EXP-002 cadence sweep: 24 / 4 / 1 / 0.25). "
                             "The corpus timeline is fixed; this only changes "
                             "how finely B/C poll it and how often A's "
                             "perception cycle runs. Default 4.0 = EXP-001.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--harness-config", type=Path,
                        help="JSON file with ONE harness config dict for this run")
    parser.add_argument("--out", type=Path, default=Path("exp001-out"))
    parser.add_argument("--smoke", action="store_true",
                        help="run every condition once against the hermetic "
                             "oracle harness; no network, no spend")
    args = parser.parse_args(argv)

    harness_cfg = None
    if args.harness_config:
        harness_cfg = json.loads(args.harness_config.read_text(encoding="utf-8"))
    if args.smoke:
        harness_cfg = oracle_config()

    args.out.mkdir(parents=True, exist_ok=True)
    conditions = ["A", "B", "C"] if args.smoke else [args.condition]
    if not conditions[0]:
        parser.error("--condition is required unless --smoke")
    runs = 1 if args.smoke else args.runs

    all_metrics = []
    for cond in conditions:
        for k in range(runs):
            # In smoke mode condition A stays dry-run: a non-dry A cycle
            # goes through the REAL action gate (budget check + model call).
            # B/C run the oracle subprocess for real — it's hermetic.
            dry = args.dry_run or (args.smoke and cond == "A")
            m = run_once_for_condition(cond, k, args.seed + k, args.out,
                                       dry_run=dry,
                                       tick_hours=args.tick_hours,
                                       harness_cfg=harness_cfg)
            all_metrics.append(m)
            print(f"{cond} run{k}: recall={m['recall']} precision={m['precision']} "
                  f"wakes={m['total_invocations']} ttd={m['time_to_detection_ticks']}")

    summary_path = args.out / "summary.json"
    existing = []
    if summary_path.exists():
        existing = json.loads(summary_path.read_text(encoding="utf-8"))
    summary_path.write_text(json.dumps(existing + all_metrics, indent=2),
                            encoding="utf-8")
    print(f"summary -> {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
