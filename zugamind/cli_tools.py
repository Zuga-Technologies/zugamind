"""Operator tools for the `zugamind` command — the commands an operator of a
money-spending daemon needs to TRUST it, beyond start/stop/status/watch.

    zugamind doctor            is my wiring sane? config, keys (presence
                               only), local model, budget, journal health,
                               stale PID, pause file. Exit 1 on any FAIL.
    zugamind logs              journalctl-style view of the journal across
                               its segments: -n, --since/--until, --kind,
                               --json, --follow.
    zugamind budget            this month's ledger against the cap.
    zugamind explain [last|N]  why did that cycle wake (or not): the winner,
                               its bid vs the modulated number, the other
                               bids, the gate it faced, what followed.
    zugamind verify            the end-to-end canary test (scripts/
                               verify_harness.py) from the installed command.

Added 2026-08-29 after a peer survey (claude doctor, openclaw doctor/logs,
journalctl, ccusage, agent-replay): every one of these answers a question
an operator previously answered by reading source or hand-parsing JSONL.
Stdlib-only. Read-only except `doctor --fix` (stale PID file only).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from foundation import config as foundation_config

# ---------------------------------------------------------------------------
# shared bits
# ---------------------------------------------------------------------------

_EVENT_KINDS = (
    "cycle", "wake_filtered", "harness_skip", "harness_invocation", "harness_rate_limited",
    "harness_rate_limit_indeterminate", "alarm", "quiet_hours_deferred", "cycle_error",
    "floor_calibrated", "floor_basis_switched", "floor_drifted", "budget_persist_failed",
    "paused", "resumed", "daemon_started", "daemon_restarted", "shutdown", "last_wake_in_future",
    "state_persist_failed", "work_claim", "handoff", "handoff_done",
)

_REL_RE = re.compile(r"^(\d+)([smhd])$")


def parse_when(value: Optional[str], now: Optional[datetime] = None) -> Optional[str]:
    """An ISO timestamp for --since/--until: absolute ISO, a relative
    shorthand (`90m`, `3h`, `2d`), or `today`. Returns the UTC ISO string
    the journal itself compares against, or None."""
    if not value:
        return None
    now = now or datetime.now(timezone.utc)
    v = value.strip().lower()
    if v == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    m = _REL_RE.match(v)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"s": timedelta(seconds=n), "m": timedelta(minutes=n),
                 "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
        return (now - delta).isoformat()
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _short_ts(ts: Any) -> str:
    s = str(ts or "?")
    return s[11:19] if len(s) >= 19 and s[10] == "T" else s[:19]


def summarize_event(ev: Dict[str, Any]) -> str:
    """One human line per journal event kind (no payload dumps)."""
    kind = ev.get("kind", "?")
    if kind == "cycle":
        w = ev.get("winner") or {}
        bids = ev.get("bids") or []
        if not w:
            return f"cycle: no winner ({len(bids)} bids)"
        sal = w.get("salience")
        raw = (w.get("context") or {}).get("raw_salience")
        sal_s = f"{sal:.3f}" if isinstance(sal, (int, float)) else "?"
        raw_s = f" (bid {raw:.3f})" if isinstance(raw, (int, float)) and raw != sal else ""
        return f"cycle: {w.get('source_module', '?')} {sal_s}{raw_s}  {len(bids)} bids  {str(w.get('content', ''))[:80]}"
    if kind == "harness_invocation":
        status = "ok" if ev.get("ok") else f"FAILED {ev.get('error', '?')}"
        return f"wake: {ev.get('harness', '?')} {status}{' (dry-run)' if ev.get('dry_run') else ''}"
    if kind == "wake_filtered":
        sal = ev.get("salience")
        sal_s = f" {sal:.3f}" if isinstance(sal, (int, float)) else ""
        filters = ev.get("filters") or []
        if filters:
            why = "; ".join(f"{f.get('harness')}: {f.get('reason')}" for f in filters if isinstance(f, dict))
            return f"filtered: {ev.get('winner_module', '?')}{sal_s} — {why[:160]}"
        return (f"filtered: {ev.get('winner_module', ev.get('module', '?'))}{sal_s} — no enabled harness "
                f"accepted it (module filter or wake floor; `zugamind explain` shows which)")
    if kind == "harness_skip":
        return f"skip: {str(ev.get('reason', ev.get('failure_reason', '')))[:100]}"
    if kind == "alarm":
        return f"ALARM: {str(ev.get('detail', ''))[:100]}"
    if kind == "quiet_hours_deferred":
        w = ev.get("winner") or {}
        return f"deferred (quiet hours): {ev.get('harness', '?')} <- {w.get('source_module', '?')}"
    if kind == "harness_rate_limited":
        return (f"rate limited: {ev.get('harness', '?')} {ev.get('window', '?')} cap "
                f"{ev.get('recent_count')}/{ev.get('max_per_hour') or ev.get('max_per_day')}")
    if kind in ("floor_calibrated", "floor_drifted", "floor_basis_switched"):
        return (f"{kind}: {ev.get('harness', '?')} floor={ev.get('floor', ev.get('to', ev.get('raw_floor')))}"
                f"{' AT CEILING' if ev.get('at_ceiling') else ''}")
    if kind == "cycle_error":
        phase = f" [{ev['phase']}]" if ev.get("phase") else ""
        return f"CYCLE ERROR{phase}: {str(ev.get('error', ''))[:120]}"
    if kind == "daemon_restarted":
        return (f"daemon RESTARTED (pid {ev.get('pid')}) — previous run ended without a shutdown event; "
                f"last seen {ev.get('last_event_kind')} @ {_short_ts(ev.get('last_event_ts'))}")
    if kind == "daemon_started":
        return f"daemon started (pid {ev.get('pid')}{', dry-run' if ev.get('dry_run') else ''})"
    if kind == "shutdown":
        return f"daemon shutdown ({ev.get('reason', '?')})"
    if kind in ("handoff", "handoff_done"):
        return f"{kind}: {ev.get('id', '?')} {str(ev.get('detail', ''))[:80]}"
    extra = {k: v for k, v in ev.items() if k not in ("ts", "kind")}
    return f"{kind}: {json.dumps(extra, default=str)[:100]}" if extra else kind


# ---------------------------------------------------------------------------
# budget
# ---------------------------------------------------------------------------

def cmd_budget(args: argparse.Namespace) -> int:
    from foundation.budget import load_budget  # noqa: WPS433 — lazy, test-patchable

    # --reconcile folds spends that were BILLED but never written into the
    # ledger back in. action_gate journals a budget_persist_failed event at
    # the moment it happens; without this, one failed write under-counts the
    # monthly cap for the rest of the month, because every later call reloads
    # budget.json fresh. --dry-run first: this moves a money number.
    # --provider asks Anthropic what it ACTUALLY billed and holds it beside
    # the local number. Different gap from --reconcile: that one repairs
    # spends we know we made and failed to write down; this one catches a
    # per-call cost ESTIMATE that is simply wrong, which drifts the ledger a
    # little on every call and leaves no trace anywhere on disk. It needs an
    # Admin credential, so it always reports what it found under which env
    # var name first -- that line is the answer to "do I have the right key",
    # and it never prints the key.
    if getattr(args, "provider", False):
        from foundation.cost_report import compare  # noqa: WPS433

        summary = compare()
        if getattr(args, "json", False):
            print(json.dumps(summary, indent=2))
            return 0 if summary.get("ok") else 1

        cred = summary["credential"]
        if cred["present"]:
            print(f"credential: {cred['name']}  kind={cred['kind']}  "
                  f"{cred['length']} chars")
            print(f"            {cred['detail']}")
        else:
            print("credential: none found")
            print(f"            {cred['detail']}")
        window = summary["window"]
        print(f"window:     {window['starting_at'][:10]} -> "
              f"{window['ending_at'][:10]}   scope: {summary['scope']}")

        if not summary.get("ok"):
            print(f"result:     {summary['verdict']}")
            if summary.get("error"):
                print(f"            ({summary['error']})")
            return 1

        print(f"provider:   ${summary['provider_usd']:.4f}"
              f"   ({summary['buckets']} daily bucket(s))")
        print(f"ledger:     ${summary['ledger_usd']:.4f}")
        if summary.get("drift_usd") is not None:
            pct = summary.get("drift_pct")
            tail = f"  ({pct:+.1f}%)" if pct is not None else ""
            print(f"drift:      ${summary['drift_usd']:+.4f}{tail}")
        for name, amount in sorted(summary.get("by_workspace", {}).items(),
                                   key=lambda kv: -kv[1]):
            print(f"    {name:<32} ${amount:.4f}")
        print(f"verdict:    {summary['verdict']}")
        return 0

    if getattr(args, "reconcile", False) or getattr(args, "dry_run", False):
        from foundation.budget_reconcile import reconcile  # noqa: WPS433

        summary = reconcile(dry_run=bool(getattr(args, "dry_run", False)))
        if getattr(args, "json", False):
            print(json.dumps(summary, indent=2))
            return 0
        if not summary["events"]:
            print("nothing to reconcile — every recorded spend reached the ledger")
            return 0
        verb = "would fold" if summary["dry_run"] else "folded"
        print(f"{verb} ${summary['amount']:.4f} from {summary['events']} "
              f"unrecorded spend(s) into the ledger")
        for name, amount in sorted(summary["by_caller"].items(),
                                   key=lambda kv: -kv[1]):
            print(f"    {name:<32} ${amount:.4f}")
        if summary["dry_run"]:
            print("  (dry run — nothing written; re-run with --reconcile)")
        return 0

    b = load_budget()
    cap = foundation_config.monthly_cap()
    if getattr(args, "json", False):
        print(json.dumps({**b, "cap": cap}, indent=2))
        return 0
    calls = b.get("calls") or {}
    spent = float(b.get("paid_spent", 0.0))
    remaining = float(b.get("remaining", cap))
    pct = (spent / cap * 100.0) if cap else 0.0
    fresh = " (no spend yet this month)" if spent == 0 and not any(calls.values()) else ""
    print(f"month: {b.get('month', '?')}          cap: ${cap:.2f}{fresh}")
    print(f"spent: ${spent:.4f}            remaining: ${remaining:.2f}  ({pct:.1f}% used)")
    print("calls: " + "  ".join(f"{k}={int(v)}" for k, v in calls.items()))
    if spent > 0:
        day = datetime.now().day
        per_day = spent / max(1, day)
        if per_day > 0:
            days_left = remaining / per_day
            print(f"pace: ${per_day:.4f}/day -> cap in ~{days_left:.0f} day(s) at this pace")
    return 0


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------

def cmd_logs(args: argparse.Namespace) -> int:
    from continuity import journal as _journal  # noqa: WPS433
    kinds = {k.strip() for k in (args.kind or "").split(",") if k.strip()}
    since = parse_when(args.since)
    until = parse_when(args.until)
    events = _journal.read_events(since_iso=since, limit=None)
    if until:
        events = [e for e in events if str(e.get("ts", "")) <= until]
    if kinds:
        events = [e for e in events if e.get("kind") in kinds]
    oldest_live = _journal.read_events(limit=None)
    if since and oldest_live and since < str(oldest_live[0].get("ts", "")) and _journal.archive_files():
        print(f"(note: --since predates the live segments; {len(_journal.archive_files())} archived "
              f"segment(s) exist and are not scanned — see journal.archive.*.jsonl)", file=sys.stderr)
    n = args.n if args.n is not None else 20
    shown = events if n == 0 else events[-n:]
    for ev in shown:
        if args.json:
            print(json.dumps(ev, default=str))
        else:
            print(f"[{_short_ts(ev.get('ts'))}] {summarize_event(ev)}")
    if not args.follow:
        return 0
    # follow: the same rotation-aware tail `watch` uses
    import cli as _cli  # noqa: WPS433
    pos = _journal.JOURNAL_FILE.stat().st_size if _journal.JOURNAL_FILE.exists() else 0
    try:
        while True:
            time.sleep(1)
            new, pos, rotated = _cli._tail(_journal.JOURNAL_FILE, pos)
            if rotated:
                print("(journal rotated — following the fresh file)", file=sys.stderr)
            for line in new.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if kinds and ev.get("kind") not in kinds:
                    continue
                print(json.dumps(ev, default=str) if args.json else f"[{_short_ts(ev.get('ts'))}] {summarize_event(ev)}")
                sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

Check = Tuple[str, str, str]  # (level OK|WARN|FAIL, name, detail)


def _ollama_check() -> Tuple[bool, str]:
    """(reachable, detail). Kept as a seam so tests never touch the network."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"{foundation_config.OLLAMA_URL}/api/tags", timeout=3) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        names = [m.get("name", "") for m in data.get("models", []) if isinstance(m, dict)]
        want = foundation_config.LOCAL_MODEL
        norm = lambda s: s if ":" in s else f"{s}:latest"  # noqa: E731
        if any(norm(want) == norm(n) for n in names):
            return True, f"reachable, {want} installed"
        return False, f"reachable but {want!r} is NOT installed (have: {', '.join(names) or 'none'})"
    except Exception as e:  # noqa: BLE001
        return False, f"unreachable at {foundation_config.OLLAMA_URL} ({e})"


def run_doctor(fix: bool = False, ollama: Optional[Callable[[], Tuple[bool, str]]] = None) -> List[Check]:
    ollama = ollama or _ollama_check  # resolved at call time so tests can patch the seam
    checks: List[Check] = []
    add = checks.append
    engine = foundation_config.ENGINE_DIR
    src = "ZUGAMIND_DATA_DIR" if os.environ.get("ZUGAMIND_DATA_DIR") else "default — set ZUGAMIND_DATA_DIR to point elsewhere"
    add(("OK", "data dir", f"{engine} ({src})"))

    # 1. harness config
    try:
        from act import command_actuator  # noqa: WPS433
        from cognition.workspace import workspace_modules  # noqa: WPS433
        cfg_path = command_actuator._config_path()
        if not cfg_path.exists():
            add(("FAIL", "harness config", f"{cfg_path} not found — copy one from examples/harness-configs/"))
        else:
            configs = command_actuator.load_harness_configs(cfg_path)
            enabled = [c for c in configs if c.get("enabled", True)]
            module_names = {m.name for m in workspace_modules.ALL_MODULES}
            if not configs:
                add(("FAIL", "harness config", f"{cfg_path.name}: no valid harness entries (see the log for why)"))
            elif not enabled:
                add(("WARN", "harness config", f"{len(configs)} harness(es), none enabled — nothing will ever wake"))
            else:
                add(("OK", "harness config", f"{len(enabled)} enabled of {len(configs)}"))
            for c in enabled:
                argv0 = str((c.get("command") or ["?"])[0])
                found = shutil.which(argv0) or Path(argv0).exists()
                add(("OK" if found else "FAIL", f"harness {c['name']} command",
                     f"{argv0} {'found' if found else 'NOT found on PATH'}"))
                unknown = [m for m in (c.get("wake_modules") or []) if m not in module_names]
                if unknown:
                    add(("WARN", f"harness {c['name']} wake_modules",
                         f"unknown module(s) {unknown} — this filter can never match"))
                if "{briefing_file}" not in " ".join(map(str, c.get("command") or [])):
                    add(("WARN", f"harness {c['name']} command",
                         "no {briefing_file} placeholder — the harness will not receive the briefing"))
    except Exception as e:  # noqa: BLE001
        add(("FAIL", "harness config", f"could not load: {e}"))

    # 2. keys (presence only — never the value)
    add(("OK" if os.environ.get("ANTHROPIC_API_KEY") else "WARN", "ANTHROPIC_API_KEY",
         "set" if os.environ.get("ANTHROPIC_API_KEY") else "not set — paid tiers (haiku/sonnet/opus) will return None"))

    tier = os.environ.get("ZUGAMIND_WAKE_TIER", "").strip()
    if tier and tier not in ("local", "haiku", "sonnet", "opus"):
        add(("FAIL", "ZUGAMIND_WAKE_TIER", f"{tier!r} is not a tier (local/haiku/sonnet/opus) — every wake is refused until fixed"))
    elif tier:
        add(("OK", "ZUGAMIND_WAKE_TIER", f"{tier} (the wake decision runs there)"))

    # 3. local model
    ok, detail = ollama()
    add(("OK" if ok else "WARN", "local model (Ollama)", detail))

    # 4. budget
    try:
        from foundation.budget import load_budget  # noqa: WPS433
        b = load_budget()
        cap = foundation_config.monthly_cap()
        remaining = float(b.get("remaining", cap))
        month = datetime.now().strftime("%Y-%m")
        if remaining < 0:
            add(("FAIL", "budget", f"remaining ${remaining:.2f} < 0 — the ledger is over the cap"))
        elif remaining < cap * 0.1:
            add(("WARN", "budget", f"${remaining:.2f} of ${cap:.2f} left this month"))
        else:
            add(("OK", "budget", f"${remaining:.2f} of ${cap:.2f} left ({b.get('month', month)})"))
    except Exception as e:  # noqa: BLE001
        add(("FAIL", "budget", f"budget.json unreadable: {e}"))

    # 5. state
    try:
        from foundation.state import load_state  # noqa: WPS433
        st = load_state()
        add(("OK", "state", f"{st.get('state', '?')}, last wake {st.get('last_wake') or 'never'}"))
    except Exception as e:  # noqa: BLE001
        add(("FAIL", "state", f"state.json unreadable: {e}"))

    # 6. journal health
    try:
        from continuity import journal as _journal  # noqa: WPS433
        segs = [s for s in (_journal.previous_segment(), _journal.JOURNAL_FILE) if s.exists()]
        if not segs:
            add(("OK", "journal", "no journal yet (fresh install)"))
        else:
            bad = 0
            total = 0
            for seg in segs:
                raw = seg.read_bytes()
                if raw and not raw.endswith(b"\n"):
                    add(("WARN", f"journal {seg.name}", "torn tail (last write did not finish) — healed on next append"))
                for line in raw.decode("utf-8", "replace").splitlines():
                    if not line.strip():
                        continue
                    total += 1
                    try:
                        json.loads(line)
                    except Exception:
                        bad += 1
            size = sum(s.stat().st_size for s in segs)
            level = "WARN" if bad else "OK"
            add((level, "journal", f"{total} events in {len(segs)} segment(s), {size / 1024:.0f} KB"
                 + (f", {bad} malformed line(s) (unrecoverable, skipped on read)" if bad else "")
                 + (f", {len(_journal.archive_files())} archived" if _journal.archive_files() else "")))
    except Exception as e:  # noqa: BLE001
        add(("FAIL", "journal", f"unreadable: {e}"))

    # 7. pause file
    if foundation_config.PAUSE_FILE.exists():
        add(("WARN", "pause", f"{foundation_config.PAUSE_FILE} exists — the daemon is paused"))
    else:
        add(("OK", "pause", "not paused"))

    # 8. PID file
    try:
        import cli as _cli  # noqa: WPS433
        pid = _cli._read_pid()
        if pid is None:
            add(("OK", "daemon", "not running (no PID file)"))
        elif _cli._pid_alive(pid) and _cli._pid_is_ours(pid):
            add(("OK", "daemon", f"running (PID {pid})"))
        elif _cli._pid_alive(pid):
            if fix:
                _cli.PID_FILE.unlink(missing_ok=True)
                add(("OK", "daemon", f"PID file pointed at PID {pid}, which is alive but is NOT this daemon (recycled PID) — removed"))
            else:
                add(("WARN", "daemon", f"PID {pid} is alive but is NOT this daemon (a recycled PID) — "
                                       f"`zugamind stop` would kill the wrong process; `zugamind doctor --fix` removes the file"))
        else:
            if fix:
                _cli.PID_FILE.unlink(missing_ok=True)
                add(("OK", "daemon", f"stale PID file for dead PID {pid} removed"))
            else:
                add(("WARN", "daemon", f"stale PID file (PID {pid} is dead) — `zugamind doctor --fix` removes it"))
    except Exception as e:  # noqa: BLE001
        add(("WARN", "daemon", f"could not check: {e}"))

    # 9. floor calibration state
    try:
        from act import floor_calibration  # noqa: WPS433
        if floor_calibration.STATE_FILE.exists():
            state = floor_calibration._load_state()
            for name, entry in state.items():
                floor, basis = floor_calibration.resolve_gate(name)
                level = "WARN" if floor >= floor_calibration.FLOOR_CEILING else "OK"
                add((level, f"wake floor {name}", f"{floor:.3f} on the {basis} series"
                     + (" — AT CEILING: nothing wakes" if level == "WARN" else "")))
    except Exception as e:  # noqa: BLE001
        add(("WARN", "wake floor", f"could not read: {e}"))

    # 10. data dir writable
    try:
        engine.mkdir(parents=True, exist_ok=True)
        probe = engine / ".doctor-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add(("OK", "data dir writable", "yes"))
    except Exception as e:  # noqa: BLE001
        add(("FAIL", "data dir writable", f"no: {e}"))
    return checks


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = run_doctor(fix=getattr(args, "fix", False))
    if getattr(args, "json", False):
        print(json.dumps([{"level": l, "check": n, "detail": d} for l, n, d in checks], indent=2))
    else:
        for level, name, detail in checks:
            print(f"[{level:<4}] {name}: {detail}")
        fails = sum(1 for l, _, _ in checks if l == "FAIL")
        warns = sum(1 for l, _, _ in checks if l == "WARN")
        print(f"\n{len(checks)} checks: {fails} FAIL, {warns} WARN")
    return 1 if any(l == "FAIL" for l, _, _ in checks) else 0


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------

def explain_cycle(which: str = "last") -> List[str]:
    """Lines explaining the Nth-from-last cycle that had a winner and what
    followed it (wake / filtered / skipped / deferred / rate limited)."""
    from continuity import journal as _journal  # noqa: WPS433
    events = _journal.read_events(limit=None)
    cycles = [i for i, e in enumerate(events) if e.get("kind") == "cycle" and e.get("winner")]
    if not cycles:
        return ["no cycle with a winner in the journal yet"]
    back = 0 if which in ("last", "1") else max(0, int(which) - 1)
    if back >= len(cycles):
        return [f"only {len(cycles)} cycle(s) with a winner in the journal"]
    idx = cycles[-1 - back]
    ev = events[idx]
    w = ev.get("winner") or {}
    ctx = w.get("context") or {}
    sal, raw = w.get("salience"), ctx.get("raw_salience")
    out = [f"cycle @ {ev.get('ts')}  ({back + 1} from last)",
           f"winner: {w.get('source_module')}  {str(w.get('content', ''))[:120]}"]
    if isinstance(sal, (int, float)):
        line = f"salience: {sal:.3f}"
        if isinstance(raw, (int, float)) and abs(raw - sal) > 1e-9:
            line += f"  (the module bid {raw:.3f}; attention-health modulation {'raised' if sal > raw else 'lowered'} it)"
        out.append(line)
    if ctx.get("alarm_lane"):
        out.append("alarm lane: yes — a critical trigger bypassed the lottery and the wake floor")
    bids = ev.get("bids") or []
    if bids:
        out.append("bids: " + ", ".join(f"{b.get('module')}={b.get('salience')}" for b in bids))
    if w.get("runner_up_module"):
        out.append(f"runner-up: {w['runner_up_module']}")
    out.extend(_gate_verdicts(w))
    # what followed, up to the next cycle
    after = []
    for e in events[idx + 1:]:
        if e.get("kind") == "cycle":
            break
        after.append(f"  -> [{_short_ts(e.get('ts'))}] {summarize_event(e)}")
    out.append("then:" if after else "then: nothing journaled before the next cycle (no wake attempted)")
    out.extend(after)
    return out


def _gate_verdicts(winner: Dict[str, Any]) -> List[str]:
    """One line per enabled harness: did this winner clear its module filter
    and its wake floor? This is the sentence the journal's `wake_filtered`
    event leaves implicit."""
    from continuity import journal as _journal  # noqa: WPS433
    module = winner.get("source_module")
    sal, raw = winner.get("salience"), (winner.get("context") or {}).get("raw_salience")
    lines: List[str] = []
    try:
        from act import command_actuator  # noqa: WPS433
        configs = [c for c in command_actuator.load_harness_configs() if c.get("enabled", True)]
    except Exception:  # noqa: BLE001
        configs = []
    if not configs:
        lines.append("gate: no enabled harness — nothing can wake (see `zugamind doctor`)")
        return lines
    try:
        floors = {n: (f, b) for n, f, b in _journal._wake_gate_hints(module, [c.get("name", "") for c in configs])}
    except Exception:  # noqa: BLE001
        floors = {}
    for c in configs:
        name = c.get("name", "?")
        allowed = c.get("wake_modules") or []
        if allowed and module not in allowed:
            lines.append(f"gate {name}: module filter {allowed} does not include {module} -> cannot wake")
            continue
        min_sal = c.get("wake_min_salience")
        if isinstance(min_sal, (int, float)) and isinstance(sal, (int, float)) and sal < min_sal:
            lines.append(f"gate {name}: salience {sal:.3f} < wake_min_salience {min_sal:.3f} -> cannot wake")
            continue
        if name in floors:
            floor, basis = floors[name]
            value = raw if (basis == "raw" and isinstance(raw, (int, float))) else sal
            if isinstance(value, (int, float)):
                if value < floor:
                    lines.append(f"gate {name}: {basis} {value:.3f} < floor {floor:.3f} (short by {floor - value:.3f}) -> cannot wake")
                else:
                    lines.append(f"gate {name}: {basis} {value:.3f} >= floor {floor:.3f} -> clears the floor")
                continue
            lines.append(f"gate {name}: floor {floor:.3f} on the {basis} series")
        else:
            lines.append(f"gate {name}: no wake floor recorded yet (warm-up)")
    return lines


def cmd_explain(args: argparse.Namespace) -> int:
    for line in explain_cycle(getattr(args, "which", "last") or "last"):
        print(line)
    return 0


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

def cmd_verify(args: argparse.Namespace) -> int:
    script = foundation_config.ZUGAMIND_DIR.parent / "scripts" / "verify_harness.py"
    if not script.exists():
        print(f"verify_harness.py not found at {script} — run from a source checkout")
        return 1
    return subprocess.call([sys.executable, str(script)] + list(getattr(args, "rest", None) or []))


# ---------------------------------------------------------------------------
# report — the agent's account of its own work, judged before it is shown
# ---------------------------------------------------------------------------

def cmd_report(args: argparse.Namespace) -> int:
    """Compose a report from the journal and show it ONLY if both narrative
    gates let it through. A refusal prints the stage and reason, never the
    draft: the draft is in the journal (`logs --kind report_suppressed`).
    Exit 1 on suppression so a script can tell the two apart."""
    from cognition.reports import compose_report, emit_report

    minutes = int(getattr(args, "minutes", 60) or 60)
    draft = compose_report(window_minutes=minutes)
    if not draft:
        print(f"nothing in the journal for the last {minutes} minutes")
        return 0
    verdict = emit_report(draft, window_minutes=minutes)
    if getattr(args, "json", False):
        print(json.dumps({**verdict, "text": draft if verdict["emitted"] else None}, default=str))
        return 0 if verdict["emitted"] else 1
    if verdict["emitted"]:
        print(draft)
        return 0
    print(f"REPORT SUPPRESSED at {verdict['stage']}: {verdict['reason']}")
    for sentence in verdict.get("unbacked") or []:
        print(f"  unbacked claim: {sentence}")
    print("  (draft kept in the journal: zugamind logs --kind report_suppressed)")
    return 1


# ---------------------------------------------------------------------------
# self-mod — rewrite one facet's runtime override, under the 24h cooldown
# ---------------------------------------------------------------------------

def cmd_self_mod(args: argparse.Namespace) -> int:
    """First caller of cognition.self_mod.propose. Applies only when
    ZUGAMIND_SELF_MOD_ENABLED is on; otherwise records the proposal. Exit 1
    on any refusal (cooling, unknown facet, empty text, cannot lock)."""
    from cognition.self_mod import (ARM_WINDOW_SEC, ACTOR_HUMAN, FACETS, arm,
                                    disarm, propose)

    # --arm is the human moment the flag alone never provided: it opens a
    # window, it expires, and the agent can only write inside it.
    if getattr(args, "disarm", False):
        print("self-mod DISARMED — proposals will be recorded, not applied")
        disarm()
        return 0
    if getattr(args, "arm", False):
        r = arm()
        if not r["armed"]:
            print(f"could not arm: {r['reason']}")
            return 1
        print(f"self-mod ARMED for {int(ARM_WINDOW_SEC // 60)} min — the agent "
              f"may APPLY an override until it expires")
        return 0
    if not args.facet or not args.text_file or not args.why:
        print("usage: zugamind self-mod <facet> <text_file> --why WHY   "
              "(or --arm / --disarm)")
        return 1

    try:
        text = Path(args.text_file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cannot read {args.text_file}: {exc}")
        return 1
    # ACTOR_HUMAN: this is a person at a terminal, so neither the agent's 24h
    # cooldown nor the arming window applies. The invocation IS the human
    # moment, and a lock the agent just burned must never be the thing that
    # stops the operator undoing it.
    v = propose(args.facet, text, why=args.why,
                evidence=getattr(args, "evidence", "") or "",
                actor=ACTOR_HUMAN)
    if v["applied"]:
        print(f"applied: {v['facet']} override rewritten -> {v['path']}")
        print("  cooling 24h; the previous text is in the audit log; `rm` the file to revert fully")
        return 0
    if v["reason"] == "disabled":
        print(f"recorded, NOT applied: ZUGAMIND_SELF_MOD_ENABLED is off "
              f"({v['facet']} is now cooling 24h anyway; proposal is in the audit log)")
        return 0
    if v["reason"] == "cooling":
        print(f"refused: {v['facet']} is cooling -- {v['remaining_seconds'] / 3600:.1f}h left")
        return 1
    if v["reason"] == "unknown_facet":
        print(f"refused: unknown facet {args.facet!r}; one of {', '.join(FACETS)}")
        return 1
    print(f"refused: {v['reason']}")
    return 1


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def register(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser("doctor", help="is my wiring sane? config, keys (presence), model, budget, journal")
    p.add_argument("--fix", action="store_true", help="remove a stale PID file (the only fix applied)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("logs", help="view the journal: -n, --since/--until, --kind, --json, --follow")
    p.add_argument("-n", type=int, default=None, help="last N events (default 20; 0 = all)")
    p.add_argument("--since", help="ISO time, or 90m / 3h / 2d / today")
    p.add_argument("--until", help="ISO time, or 90m / 3h / 2d / today")
    p.add_argument("--kind", help="comma-separated kinds, e.g. cycle,harness_invocation ("
                               + ", ".join(_EVENT_KINDS[:6]) + ", ...)")
    p.add_argument("--json", action="store_true", help="raw NDJSON, one event per line")
    p.add_argument("-f", "--follow", action="store_true", help="keep tailing (rotation-aware)")
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("budget", help="this month's spend against the cap")
    p.add_argument("--json", action="store_true")
    p.add_argument("--reconcile", action="store_true",
                   help="fold spends that were billed but never written "
                        "(budget_persist_failed) back into the ledger")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="show what --reconcile would fold, and write nothing")
    p.add_argument("--provider", action="store_true",
                   help="cross-check the ledger against Anthropic's cost "
                        "report (needs an Admin credential; reports which "
                        "one it found without printing it)")
    p.set_defaults(func=cmd_budget)

    p = sub.add_parser("explain", help="why did that cycle wake (or not)")
    p.add_argument("which", nargs="?", default="last", help="'last' or N (Nth from last winning cycle)")
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("verify", help="end-to-end canary wake test (scripts/verify_harness.py)")
    p.add_argument("rest", nargs=argparse.REMAINDER, help="passed through, e.g. --dry-run")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("report", help="what did the agent do lately? composed from the journal, "
                                      "judged (work_claim + local model) before it is shown")
    p.add_argument("--minutes", type=int, default=60, help="window to report on (default 60)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("self-mod", help="rewrite one facet's runtime override (data/overrides/<facet>.md) "
                                        "under the 24h per-file cooldown; applies only if "
                                        "ZUGAMIND_SELF_MOD_ENABLED, else records the proposal")
    p.add_argument("facet", nargs="?", help="sentinel | deliberative")
    p.add_argument("text_file", nargs="?",
                   help="file whose contents become the new override")
    p.add_argument("--arm", action="store_true",
                   help="open the human arming window (default 60 min) so the "
                        "agent may APPLY an override; without it a proposal is "
                        "recorded but nothing is written")
    p.add_argument("--disarm", action="store_true", help="close the window now")
    p.add_argument("--why", help="one line: why this change (required unless --arm/--disarm)")
    p.add_argument("--evidence", default="", help="what backs it (journal ids, a reflection, ...)")
    p.set_defaults(func=cmd_self_mod)


__all__ = ["register", "run_doctor", "explain_cycle", "summarize_event", "parse_when",
           "cmd_doctor", "cmd_logs", "cmd_budget", "cmd_explain", "cmd_verify", "cmd_report",
           "cmd_self_mod"]
