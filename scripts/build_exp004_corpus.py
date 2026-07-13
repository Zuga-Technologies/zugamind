#!/usr/bin/env python3
"""Build EXP-004 corpora — multi-source loads for the strong-baseline test.

Design: docs/experiments/exp-004-strong-baseline-gates.md
Predictions (pre-registered before this file existed): exp-004-predictions.md

One corpus per source count S in {2, 4, 8, 12}: a mix of CHATTY sources
(bid most ticks at moderate salience, each with its own noise profile) and
QUIET sources (occasional low-salience bids), plus:

  - one planted incident per source-group (ZM-EXP004-C## ids, EXP-001's
    canary scheme) at seed-derived ticks — critical urgency, the ground
    truth both conditions are graded on;
  - the H4 event: a NEW source appears mid-window (tick >= N_TICKS//2) that
    neither condition has configuration for, and emits its own planted
    incident two ticks after its first chatter. Condition A needs no config
    by construction; condition E meets it with its documented default
    threshold. Whatever happens is the H4 measurement.

Synthetic-by-construction, like EXP-003 (see that design's threats section).
The chatty-source noise profiles are deliberately heterogeneous — per-source
gate thresholds must actually be tuned per source (the steelman's published
tuning procedure), not one global constant.

Same JSONL row shape as exp001/exp003 corpora:
    {"is_canary": false, "tick": N, "trigger": {...}}
    {"is_canary": true, "canary_id": "ZM-EXP004-C01", "trigger": {...}}

Usage:
    python scripts/build_exp004_corpus.py --sources 4 --seed 20260712
    python scripts/build_exp004_corpus.py --sources 4 --stats
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

N_TICKS = 42  # simulated week at 4h/tick, same grid as EXP-001/002/003

# Noise profiles: (noise type, incident type, base novelty/relevance/urgency,
# bid probability per tick). BOTH types must exist in the workspace's
# TRIGGER_TYPE_TO_MODULE routing table (workspace_modules.py) or condition A
# silently drops the trigger and the comparison is meaningless — verified
# against the table 2026-07-12. Sources deliberately spread across ALL SIX
# example modules, and profiles are heterogeneous ON PURPOSE: per-source gate
# thresholds must actually need per-source tuning. Salience jitter applied
# per-event from the run seed.
_CHATTY_PROFILES = [
    # (noise_type,             incident_type,        nov,  rel,  urg,  p_bid)
    ("production_degraded",    "production_down",    0.40, 0.50, 0.30, 0.90),
    ("daemon_task_complete",   "daemon_task_failed", 0.55, 0.45, 0.25, 0.75),
    ("code_change",            "code_change",        0.35, 0.55, 0.35, 0.60),
    ("cron_output",            "cron_output",        0.50, 0.40, 0.30, 0.80),
    ("shared_memory_update",   "vault_change",       0.45, 0.50, 0.28, 0.70),
    ("repo_issue",             "repo_issue",         0.38, 0.42, 0.22, 0.85),
]
_QUIET_PROFILES = [
    # Ordered so sources occupy DISTINCT workspace modules until source count
    # exceeds the module count (S>=8): interleaved with _CHATTY_PROFILES,
    # quiet[i] must not share a module with chatty[j<=i]. Module collisions
    # are a real dynamic the experiment SHOULD measure — but only where the
    # source count forces them, not as a low-S ordering accident (first
    # smoke had both S=2 sources in the infrastructure module, which let
    # streak-dampening bury the quiet source's critical incident below the
    # alarm lane's ALARM_MIN_SALIENCE — see the results doc for why that
    # interaction matters at S>=8).
    ("analytics_significant",  "analytics_significant", 0.50, 0.45, 0.20, 0.08),  # schedule
    ("vault_change",           "vault_change",       0.55, 0.35, 0.18, 0.12),     # knowledge
    ("git_commit",             "code_change",        0.40, 0.35, 0.15, 0.08),     # code_changes
    ("daemon_task_started",    "daemon_task_failed", 0.45, 0.50, 0.20, 0.10),     # daemon
    ("system_health",          "local_service_down", 0.60, 0.40, 0.15, 0.10),     # infrastructure
    ("environment_health",     "local_systemic_failure", 0.42, 0.40, 0.18, 0.12), # infrastructure
]

# H4 newcomer: routable (infrastructure family) but belonging to NO
# configured source — condition A needs no per-source config by design;
# condition E meets it with its documented default threshold.
H4_SOURCE = "surprise_telemetry"
H4_NOISE_TYPE = "local_service_up"
H4_INCIDENT_TYPE = "local_service_down"


def source_plan(n_sources: int) -> list[dict]:
    """Deterministic source roster for a given S: half chatty, half quiet
    (chatty gets the extra slot on odd S). The H4 source is EXTRA, on top."""
    n_chatty = (n_sources + 1) // 2
    n_quiet = n_sources - n_chatty
    plan = []
    for i in range(n_chatty):
        t, inc, nov, rel, urg, p = _CHATTY_PROFILES[i % len(_CHATTY_PROFILES)]
        plan.append(dict(name=f"{t}_{i}", kind="chatty", type=t, incident_type=inc,
                         novelty=nov, relevance=rel, urgency=urg, p_bid=p))
    for i in range(n_quiet):
        t, inc, nov, rel, urg, p = _QUIET_PROFILES[i % len(_QUIET_PROFILES)]
        plan.append(dict(name=f"{t}_{i}", kind="quiet", type=t, incident_type=inc,
                         novelty=nov, relevance=rel, urgency=urg, p_bid=p))
    return plan


def _noise_event(src: dict, tick: int, rng: random.Random) -> dict:
    jit = lambda v: round(max(0.05, min(0.85, v + rng.uniform(-0.08, 0.08))), 3)
    return {
        "is_canary": False,
        "tick": tick,
        "trigger": {
            "type": src["type"],
            "source_name": src["name"],
            "detail": f"[{src['name']}] routine {src['type']} reading, tick {tick}: "
                      f"within normal band, self-resolving",
            "novelty": jit(src["novelty"]),
            "relevance": jit(src["relevance"]),
            "urgency": jit(src["urgency"]),
        },
    }


def _incident(cid: str, src_name: str, src_type: str, tick: int) -> dict:
    # src_type is the profile's incident_type VERBATIM — it must exist in the
    # workspace routing table. An earlier draft appended "_failure" here,
    # producing unroutable types that condition A's router silently dropped:
    # A could not perceive ANY planted incident and the whole first A/E run
    # set was invalidated (exp004-out-invalid/). Types must arrive routable.
    return {
        "is_canary": True,
        "canary_id": cid,
        "tick": tick,
        "trigger": {
            "type": src_type,
            "source_name": src_name,
            "detail": f"[{cid}] Monitored source {src_name} is DOWN (not the "
                      f"usual chatter). This item requires action; its id is {cid}.",
            "novelty": 1.0,
            "relevance": 1.0,
            "urgency": 0.95,
        },
    }


def build(n_sources: int, seed: int) -> tuple[list[dict], dict[str, int]]:
    rng = random.Random(seed * 1000 + n_sources)
    plan = source_plan(n_sources)
    rows: list[dict] = []

    for src in plan:
        for tick in range(N_TICKS):
            if rng.random() < src["p_bid"]:
                rows.append(_noise_event(src, tick, rng))

    # One planted incident per source (every source's real emergency must be
    # catchable — this generalizes EXP-003's H2 to all sources), at distinct
    # seed-derived ticks in the first-half/whole window.
    planted: dict[str, int] = {}
    ticks = rng.sample(range(2, N_TICKS - 2), len(plan))
    for i, (src, tick) in enumerate(zip(plan, ticks), start=1):
        cid = f"ZM-EXP004-C{i:02d}"
        rows.append(_incident(cid, src["name"], src["incident_type"], tick))
        planted[cid] = tick

    # H4: the untuned newcomer. First chatter at h4_start, its incident two
    # ticks later. Chatter salience mirrors a mid-band chatty profile.
    h4_start = rng.randrange(N_TICKS // 2, N_TICKS - 6)
    h4 = dict(name=H4_SOURCE, kind="chatty", type=H4_NOISE_TYPE,
              novelty=0.5, relevance=0.5, urgency=0.3, p_bid=0.9)
    for tick in range(h4_start, N_TICKS):
        if rng.random() < h4["p_bid"]:
            rows.append(_noise_event(h4, tick, rng))
    h4_cid = f"ZM-EXP004-C{len(plan) + 1:02d}"
    rows.append(_incident(h4_cid, H4_SOURCE, H4_INCIDENT_TYPE, h4_start + 2))
    planted[h4_cid] = h4_start + 2

    rows.sort(key=lambda r: r["tick"])
    return rows, planted


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=int, required=True,
                        choices=[2, 4, 8, 12])
    parser.add_argument("--seed", type=int, default=20260712)
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    rows, planted = build(args.sources, args.seed)
    out = args.out or (Path(__file__).resolve().parent /
                       f"exp004_corpus_s{args.sources}.jsonl")

    if args.stats:
        n_bg = sum(1 for r in rows if not r["is_canary"])
        print(f"S={args.sources}: {n_bg} noise events, {len(planted)} planted "
              f"incidents ({', '.join(f'{c}@t{t}' for c, t in sorted(planted.items()))})")
        return 0

    with open(out, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    print(f"built {out.name}: {len(rows)} rows, {len(planted)} planted incidents")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
