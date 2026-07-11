#!/usr/bin/env python3
"""Build the frozen EXP-001 corpus (scripts/exp001_corpus.jsonl).

Background events are REAL scanner output, captured live by this script and
frozen verbatim — run it several times across a day or two and it appends,
deduplicating on each trigger's stable key, so the corpus reflects genuine
event-stream texture rather than one moment's snapshot. Nothing synthetic is
ever added to the background set.

Canaries are defined HERE, in code, so their provenance is reviewable. Each
is modeled on a real incident class from the system's history (dead feeds,
silently-404ing sources, failing services — the incident that motivated the
repo) and carries a unique ID token ZM-EXP001-Cnn. Canary→tick placement is
NOT stored in the corpus; scripts/run_exp001.py assigns placement per run
from a recorded seed, per the design doc.

Once the corpus is declared frozen (committed for the measured runs), this
script should not be run against it again — regenerating after
pre-registration would invalidate the experiment.

Usage:
    python scripts/build_exp001_corpus.py            # capture + append
    python scripts/build_exp001_corpus.py --stats    # report only, no capture
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "zugamind"))

import scanners as scanners_pkg  # noqa: E402
from scanners import discover_dynamic_scanners  # noqa: E402

CORPUS_FILE = Path(__file__).resolve().parent / "exp001_corpus.jsonl"
N_TICKS = 42  # simulated week at 4h/tick
N_CANARIES = 10


def stable_key(trigger: dict) -> str:
    for k in ("story_id", "id", "issue_id", "url"):
        if trigger.get(k):
            return f"{trigger.get('type')}:{trigger[k]}"
    return "sha1:" + hashlib.sha1(str(trigger.get("detail", "")).encode()).hexdigest()


def assign_tick(key: str) -> int:
    return int(hashlib.sha1(key.encode()).hexdigest(), 16) % N_TICKS


def backfill_hn_triggers(target: int) -> list[dict]:
    """Fetch REAL HN stories from the past 7 days via the Algolia API, shaped
    exactly like the hackernews scanner's triggers.

    Corpus amendment 2026-07-11 (documented in the design doc, calibration
    note 4): the pilot revealed the captured background set (25 events) fell
    far short of the design's ~200-event spec, leaving cron ticks nearly
    empty and hypothesis H3 untestable. This backfill raises density using
    only real, verifiable events (each row carries its story_id and url) —
    nothing synthetic, canaries untouched, predictions unchanged.
    """
    import time
    from urllib.request import urlopen

    out: list[dict] = []
    week_ago = int(time.time()) - 7 * 86400
    page = 0
    while len(out) < target and page < 20:
        url = (
            "https://hn.algolia.com/api/v1/search_by_date?tags=story"
            f"&numericFilters=points>2,created_at_i>{week_ago}"
            f"&hitsPerPage=50&page={page}"
        )
        with urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        hits = data.get("hits", [])
        if not hits:
            break
        for h in hits:
            title = (h.get("title") or "").strip()
            if not title:
                continue
            out.append({
                "type": "hackernews_story",
                "detail": f"HN [{h.get('points', 0)}pts]: {title}",
                "url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
                "story_id": int(h["objectID"]),
                "score": h.get("points", 0),
                "novelty": 0.55,
                "relevance": 0.5,
                "urgency": 0.3,
            })
        page += 1
    return out[:target]


def collect_live_triggers() -> list[dict]:
    """Call every available scan_* callable once; skip failures loudly."""
    scan_fns: dict[str, object] = {}
    for name in dir(scanners_pkg):
        if name.startswith("scan_") and callable(getattr(scanners_pkg, name)):
            scan_fns[name] = getattr(scanners_pkg, name)
    try:
        scan_fns.update(discover_dynamic_scanners())
    except Exception as exc:  # fail-open for capture: report, keep going
        print(f"dynamic discovery failed: {exc}")
    triggers: list[dict] = []
    for name, fn in sorted(scan_fns.items()):
        try:
            batch = fn() or []
            print(f"{name}: {len(batch)} triggers")
            triggers.extend(batch)
        except Exception as exc:
            print(f"{name}: SKIPPED ({type(exc).__name__}: {exc})")
    return triggers


def make_canaries() -> list[dict]:
    """Ten ground-truth-important items, modeled on real incident classes."""
    incidents = [
        ("research RSS feed has returned HTTP 404 on every poll for 8 weeks; "
         "the scanner swallowed the error each time"),
        ("hackernews scanner cache file is corrupt; every scan since 03:00 has "
         "returned zero items while reporting success"),
        ("github issues source rate-limited: token expired, all polls failing "
         "with 401 since yesterday"),
        ("arxiv source DNS resolution failing; 14 consecutive scan errors "
         "suppressed by the retry wrapper"),
        ("monitored service zugamind-daemon has missed its last 6 heartbeats"),
        ("journal write failing intermittently: disk at 99%; rate-limit "
         "counting integrity at risk"),
        ("budget persistence write failed twice this hour; spend cap cannot "
         "be enforced until the ledger is repaired"),
        ("reddit source feed moved: permanent redirect since Tuesday, follower "
         "not following, zero items ingested"),
        ("scheduled backup of engine state has not run in 9 days; last "
         "snapshot predates the current journal"),
        ("vendor status page reports our pinned API version sunsets in 72h; "
         "no migration has been scheduled"),
    ]
    # Canary trigger types are spread across four workspace modules
    # (infrastructure / schedule / daemon / repo_issues). With a single type,
    # all ten canaries route to one module and the attention schema's
    # diversity cap correctly suppresses the later ones as a same-class alarm
    # streak — the oracle smoke run measured recall 0.6 in condition A from
    # product behavior, not detection failure. A real multi-day incident
    # window spans modules; a homogeneous one does not. Corpus-design choice
    # documented in the EXP-001 design doc (calibration note 3); conditions
    # B/C are unaffected (they render only the `detail` field, which is
    # unchanged). All four modules put trigger detail text into their bid
    # content, so the canary id survives into the wake briefing; all clear
    # the pre-declared wake floor (0.35) at single-trigger salience.
    incident_types = [
        ("local_service_down", "infrastructure"),   # C01 rss feed 404
        ("analytics_significant", "schedule"),      # C02 scanner zero-item anomaly
        ("repo_issue", "repo_issues"),              # C03 token expired, polls 401
        ("local_service_down", "infrastructure"),   # C04 arxiv DNS
        ("daemon_task_failed", "daemon"),           # C05 missed heartbeats
        ("local_service_down", "infrastructure"),   # C06 journal write / disk
        ("daemon_task_failed", "daemon"),           # C07 budget ledger write
        ("analytics_significant", "schedule"),      # C08 feed redirect, zero items
        ("daemon_task_failed", "daemon"),           # C09 backup not run
        ("repo_issue", "repo_issues"),              # C10 API version sunset
    ]
    canaries = []
    for i, detail in enumerate(incidents, start=1):
        cid = f"ZM-EXP001-C{i:02d}"
        ttype, _module = incident_types[i - 1]
        trigger = {
            "type": ttype,
            "service": f"exp001-{i:02d}",
            "detail": f"[{cid}] Monitored-source failure: {detail}. "
                      f"This item requires action; its id is {cid}.",
            "novelty": 1.0,
            "relevance": 1.0,
            "urgency": 0.9,
        }
        if ttype == "repo_issue":
            # RepoIssuesModule renders issue_title/issue_number/repo into its
            # bid content — the id must lead the title to survive the 200-char
            # briefing cap.
            trigger["issue_title"] = f"[{cid}] {detail} — action required ({cid})"
            trigger["issue_number"] = 9000 + i
            trigger["repo"] = "exp001/watched-repo"
        canaries.append({
            "is_canary": True,
            "canary_id": cid,
            "trigger": trigger,
        })
    return canaries


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stats", action="store_true", help="report only")
    parser.add_argument("--rebuild-canaries", action="store_true",
                        help="regenerate canary rows from make_canaries() even "
                             "if the corpus already has them (background events "
                             "are preserved)")
    parser.add_argument("--backfill-hn", type=int, default=0, metavar="N",
                        help="add up to N real HN stories from the past 7 days "
                             "(Algolia) to reach the design's ~200-event spec; "
                             "see calibration note 4 in the design doc")
    args = parser.parse_args(argv)

    existing: dict[str, dict] = {}
    has_canaries = False
    if CORPUS_FILE.exists():
        with open(CORPUS_FILE, encoding="utf-8") as fh:
            for line in fh:
                row = json.loads(line)
                if row.get("is_canary"):
                    has_canaries = True
                    existing[row["canary_id"]] = row
                else:
                    existing[stable_key(row["trigger"])] = row

    if args.stats:
        bg = [r for r in existing.values() if not r.get("is_canary")]
        print(f"corpus: {len(bg)} background events, "
              f"{sum(1 for r in existing.values() if r.get('is_canary'))} canaries, "
              f"{N_TICKS} ticks")
        by_type: dict[str, int] = {}
        for r in bg:
            t = r["trigger"].get("type", "?")
            by_type[t] = by_type.get(t, 0) + 1
        for t, n in sorted(by_type.items(), key=lambda kv: -kv[1]):
            print(f"  {t}: {n}")
        return 0

    captured = collect_live_triggers()
    if args.backfill_hn > 0:
        captured.extend(backfill_hn_triggers(args.backfill_hn))
    added = 0
    for trig in captured:
        key = stable_key(trig)
        if key in existing:
            continue
        existing[key] = {
            "is_canary": False,
            "tick": assign_tick(key),
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "trigger": trig,
        }
        added += 1

    if not has_canaries or args.rebuild_canaries:
        for row in make_canaries():
            existing[row["canary_id"]] = row

    with open(CORPUS_FILE, "w", encoding="utf-8") as fh:
        for row in existing.values():
            fh.write(json.dumps(row) + "\n")

    bg = sum(1 for r in existing.values() if not r.get("is_canary"))
    print(f"captured {len(captured)} triggers, {added} new; "
          f"corpus now {bg} background + {N_CANARIES} canaries -> {CORPUS_FILE.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
