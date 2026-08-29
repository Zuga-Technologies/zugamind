"""ZugaMind CLI — the `zugamind` command.

Installed as a console-script entry point (see pyproject.toml
`[project.scripts]`). Wraps stream.runner so the cognition loop can run
as a real detached background service instead of requiring a foreground
terminal, and so any terminal (this one, a new one, a different session
entirely) can attach to the SAME running instance and watch it live.

Subcommands:
    zugamind                 smart default: start the daemon if it isn't
                              already running, then attach and watch it
                              live. Ctrl-C only stops watching — the
                              daemon keeps running detached.
    zugamind start            start the daemon in the background, detached
                               from this terminal (survives it closing).
    zugamind stop              stop the background daemon.
    zugamind status             one-shot snapshot: running?, current state,
                                 last wake, latest event, this month's spend,
                                 journal size. No watching.
    zugamind watch               attach to a running daemon and stream its
                                  activity live (real time, timestamped).
                                  --result-file PATH (or $ZUGAMIND_RESULT_FILE)
                                  also tails the file your harness writes its
                                  result to, after each wake.
    zugamind demo                 run the zero-setup synthetic demo
                                   (python demo.py) — no daemon, no state.
    zugamind doctor | logs | budget | explain | verify
                                  operator tools — see cli_tools.py.

Stdlib only, matching the rest of this package's zero-dependency design.
Cross-platform (Windows + POSIX) detach and liveness-check, no OS branching
exposed to the caller.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from foundation import config as foundation_config  # noqa: E402

PID_FILE = foundation_config.ENGINE_DIR / "daemon.pid"
DAEMON_LOG = foundation_config.ENGINE_DIR / "daemon.log"
JOURNAL_FILE = foundation_config.ENGINE_DIR / "journal.jsonl"

BANNER = r"""
 _____   _   _  ___   _   __  __ ___ _  _ ___
|_  / | | | | |/ __| /_\ |  \/  |_ _| \| |   \
 / /| |_| | |_| | (_ |/ _ \| |\/| || || .` | |) |
/___|\___/ \___/ \___/_/ \_\_|  |_|___|_|\_|___/
"""

ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _colour_enabled() -> bool:
    """Colour only on a real terminal, never under NO_COLOR, and on Windows
    only after switching the console into VT mode — legacy conhost prints
    the escape sequences as literal garbage otherwise."""
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            handle = k32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            if k32.GetConsoleMode(handle, ctypes.byref(mode)):
                k32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:  # noqa: BLE001 — colour is cosmetic
            return False
    return True


ANSI = _colour_enabled()
RESET = "\033[0m" if ANSI else ""
DIM = "\033[2m" if ANSI else ""
BOLD = "\033[1m" if ANSI else ""
GREEN = "\033[92m" if ANSI else ""
CYAN = "\033[96m" if ANSI else ""
RED = "\033[91m" if ANSI else ""
YELLOW = "\033[93m" if ANSI else ""


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _wrap(text: str, width: int, indent: str = "", first: str = "") -> str:
    words = [w for w in text.split(" ") if w]
    if not words:
        return ""
    lines, prefix, cur = [], first, []
    for w in words:
        trial = prefix + " ".join(cur + [w])
        if cur and len(ANSI_RE.sub("", trial)) > width:
            lines.append(prefix + " ".join(cur))
            prefix, cur = indent, [w]
        else:
            cur.append(w)
    lines.append(prefix + " ".join(cur))
    return "\n".join(lines)


def render_markdown(text: str, width: int = 0) -> str:
    """Terminal rendering of a harness reply: headings, bullets, **bold**,
    `code`, wrapped to the terminal. Ported from the retired
    tools/live-wake-monitor.py — `watch` used to print the raw markdown."""
    width = width or max(20, shutil.get_terminal_size(fallback=(100, 24)).columns - 2)
    out = []
    for raw in text.split("\n"):
        if not raw.strip():
            out.append("")
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", raw)
        if m:
            out.append(_wrap(f"{BOLD}{CYAN}{m.group(2)}{RESET}", width, "  ", f"{CYAN}|{RESET} "))
            continue
        m = re.match(r"^(\s*)[-*]\s+(.*)$", raw)
        body = m.group(2) if m else raw
        body = re.sub(r"\*\*(.+?)\*\*", f"{BOLD}\\1{RESET}", body)
        body = re.sub(r"`([^`]+)`", f"{YELLOW}\\1{RESET}", body)
        out.append(_wrap(body, width, "  ", f"{CYAN}>{RESET} ") if m else _wrap(body, width))
    return "\n".join(out)


def _status_line(text: str) -> None:
    """Overwrite the current line with `text`, padded to the FULL terminal
    width. A fixed small pad (e.g. 15 spaces) is not enough — a shorter new
    message written after a longer previous one leaves stale trailing
    characters visible past the pad, which is exactly the cut-off-looking
    garbage this was producing (found via a real screenshot, not a guess).
    """
    cols = shutil.get_terminal_size(fallback=(100, 24)).columns
    visible = ANSI_RE.sub("", text)
    pad = max(0, cols - len(visible) - 1)
    sys.stdout.write("\r" + text + " " * pad)
    sys.stdout.flush()


def _safe_stdout() -> None:
    """A redirected stdout on Windows is cp1252 (or cp437) — one glyph outside
    it and `print` raises UnicodeEncodeError, and `watch > log.txt` dies the
    first time something notable happens. Replace, never raise."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001 — not a real TextIOWrapper (captured, closed)
            pass


_OWNER_CACHE: dict[int, bool] = {}


def _pid_is_ours(pid: int) -> bool:
    """Is that live PID actually the daemon, or a recycled number now owned
    by some unrelated process? Best-effort: image name on Windows, cmdline
    on POSIX. Unknown -> True (never turn a live daemon into 'not running'
    on a lookup hiccup). Cached per PID: `watch` polls this in a loop."""
    if pid in _OWNER_CACHE:
        return _OWNER_CACHE[pid]
    ours = True
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=5).stdout
            if out.strip() and not out.lower().startswith("info:"):
                image = out.split(",")[0].strip('"').lower()
                ours = "python" in image or "zugamind" in image
        else:
            proc = Path(f"/proc/{pid}/cmdline")
            if proc.exists():
                cmdline = proc.read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").lower()
            else:
                cmdline = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                                         capture_output=True, text=True, timeout=5).stdout.lower()
            if cmdline.strip():
                ours = "stream.runner" in cmdline or "zugamind" in cmdline or "python" in cmdline
    except Exception:  # noqa: BLE001
        ours = True
    _OWNER_CACHE[pid] = ours
    return ours


def _read_pid() -> Optional[int]:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _running_pid() -> Optional[int]:
    pid = _read_pid()
    if pid and _pid_alive(pid) and _pid_is_ours(pid):
        return pid
    return None


def _spawn_detached(cmd: list[str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(log_path, "a", encoding="utf-8")
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        cmd,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        cwd=str(foundation_config.ZUGAMIND_DIR),
        **kwargs,
    )
    return proc.pid


def cmd_start(args: argparse.Namespace) -> int:
    existing = _running_pid()
    if existing:
        print(f"{YELLOW}already running{RESET} (PID {existing}). "
              f"Run `zugamind watch` to attach, or `zugamind stop` first.")
        return 0

    cmd = [sys.executable, "-m", "stream.runner", "--daemon", "--interval", str(args.interval)]
    if args.dry_run:
        cmd.append("--dry-run")

    foundation_config.ENGINE_DIR.mkdir(parents=True, exist_ok=True)
    pid = _spawn_detached(cmd, DAEMON_LOG)
    PID_FILE.write_text(str(pid), encoding="utf-8")

    print(f"{GREEN}started{RESET} — PID {pid}, interval {args.interval}s"
          f"{' (dry-run)' if args.dry_run else ''}")
    print(f"{DIM}log: {DAEMON_LOG}{RESET}")
    print(f"{DIM}this keeps running after you close this terminal.{RESET}")
    print(f"{DIM}any terminal, this one or a new one, can attach with: zugamind watch{RESET}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    pid = _running_pid()
    if not pid:
        print("not running")
        PID_FILE.unlink(missing_ok=True)
        return 0
    if os.name == "nt":
        # /F is required: without it taskkill sends WM_CLOSE, which a hidden
        # console-less daemon has no message pump to receive -- the request
        # "succeeds", the process never dies, and every restart leaks a live
        # daemon (8 found racing on 2026-08-06). Check the return code too:
        # printing "stopped" unconditionally is how the leak stayed invisible.
        result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                capture_output=True)
        if result.returncode != 0:
            print(f"FAILED to stop PID {pid} (taskkill rc={result.returncode}) "
                  "-- pid file kept so you can retry")
            return 1
    else:
        os.kill(pid, signal.SIGTERM)
    PID_FILE.unlink(missing_ok=True)
    print(f"stopped (was PID {pid})")
    return 0


def _tail(path: Path, pos: int) -> tuple:
    """Read everything appended to `path` since byte `pos`; returns
    (text, new_pos, rotated). The journal RENAMES itself when it rotates,
    so the path can suddenly be a smaller, fresh file: a plain seek to the
    old offset then reads nothing until the new file grows past it —
    events silently missed. A shrink resets the cursor to 0."""
    try:
        size = path.stat().st_size
    except OSError:
        return "", 0, False
    rotated = size < pos
    if rotated:
        pos = 0
    with open(path, "rb") as f:  # bytes: the cursor is a byte offset, not a text-mode cookie
        f.seek(pos)
        new = f.read()
        return new.decode("utf-8", "replace"), f.tell(), rotated


def _wait_for_result_file(result_file: Path, rpos: int) -> int:
    """Tail a separate file the harness writes its result to (observation-
    mode harnesses append to a notes file instead of printing). Folded in
    from the retired tools/live-wake-monitor.py; returns the new cursor."""
    print(f"{YELLOW}waiting for the result to land in {result_file}...{RESET}")
    while True:
        time.sleep(1)
        new, rpos, _ = _tail(result_file, rpos)
        if new.strip():
            print(f"\n{GREEN}{BOLD}> RESULT LANDED  [{_now()}]{RESET}\n")
            print(render_markdown(new.strip()[:4000]))
            print()
            return rpos
        _status_line(f"{DIM}[{_now()}] watching {result_file.name}...{RESET}")


def cmd_status(args: argparse.Namespace) -> int:
    pid = _running_pid()
    print(f"daemon: {GREEN + 'running' + RESET if pid else RED + 'not running' + RESET}"
          + (f" (PID {pid})" if pid else ""))

    state_file = foundation_config.STATE_FILE
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            print(f"state: {BOLD}{state.get('state', '?')}{RESET}"
                  f"  since {state.get('since', '?')}")
            print(f"last wake: {state.get('last_wake') or '(none yet)'}")
        except Exception:
            print("state: (unreadable)")
    else:
        print("state: (no state file yet — hasn't run a cycle)")

    # Last event across BOTH journal segments: right after a rotation the
    # active file is empty and the newest event lives in journal.1.jsonl.
    try:
        from continuity import journal as _journal  # noqa: WPS433 — test-patchable seam
        last = _journal.read_events(limit=1)
        if last:
            ev = last[0]
            print(f"last journal event: {ev.get('kind')} @ {ev.get('ts', '?')}")
        segments = [seg for seg in (_journal.previous_segment(), _journal.JOURNAL_FILE) if seg.exists()]
        if segments:
            total = sum(seg.stat().st_size for seg in segments)
            archived = len(_journal.archive_files())
            print(f"journal: {total / 1024:.0f} KB live in {len(segments)} segment(s)"
                  + (f", {archived} archived" if archived else ""))
    except Exception:
        pass

    # Money: the one number an operator of a wake-spending daemon needs.
    try:
        from foundation.budget import load_budget  # noqa: WPS433
        from foundation.config import monthly_cap  # noqa: WPS433
        b = load_budget()
        calls = b.get("calls") or {}
        paid = sum(int(v) for k, v in calls.items() if k != "local")
        print(f"spend this month: ${float(b.get('paid_spent', 0.0)):.4f} of ${monthly_cap():.2f} cap "
              f"(${float(b.get('remaining', 0.0)):.2f} left) — "
              f"{paid} paid call(s), {int(calls.get('local', 0))} local")
    except Exception:
        pass
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    print(f"{GREEN}{BANNER}{RESET}")
    print(f"{DIM}{CYAN}live wake monitor — attach-only, nothing below is staged{RESET}")
    pid = _running_pid()
    if pid:
        print(f"{DIM}watching PID {pid} via {JOURNAL_FILE}{RESET}")
    else:
        print(f"{YELLOW}no daemon detected — run `zugamind start` first, "
              f"or just `zugamind` to do both.{RESET}")
    print(f"{DIM}started {_now()}{RESET}\n")

    if not JOURNAL_FILE.exists():
        JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        JOURNAL_FILE.touch()

    pos = JOURNAL_FILE.stat().st_size
    result_file = getattr(args, "result_file", None) or os.environ.get("ZUGAMIND_RESULT_FILE")
    result_path = Path(result_file) if result_file else None
    rpos = result_path.stat().st_size if result_path and result_path.exists() else 0

    try:
        while True:
            time.sleep(1)
            new, pos, rotated = _tail(JOURNAL_FILE, pos)
            if rotated:
                print(f"\n{DIM}(journal rotated — following the fresh file)  [{_now()}]{RESET}")
            if new:
                for line in new.strip().split("\n"):
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    kind = ev.get("kind")
                    if kind == "cycle":
                        w = ev.get("winner") or {}
                        label = w.get("source_module", "(no winner)")
                        sal = w.get("salience")
                        sal_str = f"{sal:.3f}" if isinstance(sal, (int, float)) else "-"
                        # A bidding module that DIDN'T win looks, on the
                        # overwritten status line below, identical to no
                        # activity at all — the winner-only line was the
                        # actual UX bug (found 2026-07-17 dogfooding this
                        # exact scanner against a live session). Print a
                        # persistent (non-overwritten) line for any bid from
                        # a real-signal module, win or lose, so "the
                        # workspace noticed something" stays visible even
                        # when ambient metacognition/priority_goals wins the
                        # cycle.
                        NOTABLE = {"code_changes", "repo_issues", "knowledge",
                                   "schedule", "daemon"}
                        LABELS = {
                            "code_changes": "code activity",
                            "repo_issues": "repo issue",
                            "knowledge": "knowledge update",
                            "schedule": "scheduled signal",
                            "daemon": "background task",
                        }
                        bids = ev.get("bids") or []
                        for b in bids:
                            mod = b.get("module")
                            if mod in NOTABLE:
                                won = mod == label
                                human = LABELS.get(mod, mod)
                                if won:
                                    content = (w.get("content") or "").strip()
                                    print(f"\n{GREEN}{BOLD}  * NOTICED{RESET}  "
                                          f"{DIM}[{_now()}]{RESET}")
                                    print(f"  {human} -> {content[:110]}")
                                else:
                                    print(f"  {DIM}· saw {human}, not urgent yet"
                                          f"  [{_now()}]{RESET}")
                        _status_line(f"{DIM}[{_now()}]{RESET} cycle: {label} salience={sal_str}")
                    elif kind == "alarm":
                        print(f"\n\n{RED}{BOLD}! ALARM  [{_now()}]{RESET}  {ev.get('detail')}")
                    elif kind == "harness_invocation":
                        ok = ev.get("ok")
                        tag = "WAKE FIRED" if ok else f"WAKE FAILED ({ev.get('error', '?')})"
                        colour = GREEN if ok else RED
                        print(f"\n\n{colour}{BOLD}> {tag}  [{_now()}]{RESET}  "
                              f"harness={ev.get('harness')}{' (dry-run)' if ev.get('dry_run') else ''}")
                        stdout = (ev.get("stdout") or "").strip()
                        if stdout:
                            print(render_markdown(stdout[:4000]))
                        print()
                        if result_path and ok and not ev.get("dry_run"):
                            rpos = _wait_for_result_file(result_path, rpos)
                    elif kind == "quiet_hours_deferred":
                        w = ev.get("winner") or {}
                        print(f"\n{YELLOW}~ DEFERRED (quiet hours)  [{_now()}]{RESET}  "
                              f"{ev.get('harness')} <- {w.get('source_module', '?')}")
                    elif kind == "harness_rate_limited":
                        print(f"\n{YELLOW}~ RATE LIMITED  [{_now()}]{RESET}  {ev.get('harness')} "
                              f"({ev.get('window')} cap {ev.get('recent_count')}/"
                              f"{ev.get('max_per_hour') or ev.get('max_per_day')})")
                    elif kind in ("cycle_error", "harness_rate_limit_indeterminate", "budget_persist_failed"):
                        print(f"\n{RED}{BOLD}! {kind.upper()}  [{_now()}]{RESET}  "
                              f"{str(ev.get('error') or ev.get('failure_reason') or '')[:200]}")
                    elif kind in ("paused", "resumed", "daemon_restarted", "floor_calibrated",
                                  "floor_basis_switched", "floor_drifted"):
                        print(f"\n{DIM}· {kind}  [{_now()}]{RESET}")
            else:
                _status_line(f"{DIM}[{_now()}] watching...{RESET}")
    except KeyboardInterrupt:
        print(f"\n{DIM}stopped watching (the daemon itself keeps running).{RESET}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    demo_path = foundation_config.ZUGAMIND_DIR.parent / "demo.py"
    if not demo_path.exists():
        print(f"{RED}demo.py not found at {demo_path}{RESET}")
        return 1
    return subprocess.call([sys.executable, str(demo_path)] + (args.rest or []))


def cmd_default(args: argparse.Namespace) -> int:
    if not _running_pid():
        try:
            cmd_start(args)
        except Exception as e:  # noqa: BLE001 — bad interpreter, unwritable data dir, ...
            print(f"{RED}could not start the daemon:{RESET} {e}")
            print(f"{DIM}try `zugamind doctor`, or look in {DAEMON_LOG}{RESET}")
            if not _running_pid():
                return 1
        print()
    return cmd_watch(args)


def main(argv: Optional[list[str]] = None) -> int:
    _safe_stdout()
    parser = argparse.ArgumentParser(prog="zugamind", description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    p_start = sub.add_parser("start", help="start the daemon in the background")
    p_start.add_argument("--interval", type=int, default=420)
    p_start.add_argument("--dry-run", action="store_true")
    p_start.set_defaults(func=cmd_start)

    p_stop = sub.add_parser("stop", help="stop the background daemon")
    p_stop.set_defaults(func=cmd_stop)

    p_status = sub.add_parser("status", help="one-shot status snapshot")
    p_status.set_defaults(func=cmd_status)

    p_watch = sub.add_parser("watch", help="attach and watch live")
    p_watch.add_argument("--result-file", default=None,
                         help="also tail this file for the harness's result after each wake "
                              "(default: $ZUGAMIND_RESULT_FILE)")
    p_watch.set_defaults(func=cmd_watch)

    p_demo = sub.add_parser("demo", help="run the zero-setup synthetic demo")
    p_demo.add_argument("rest", nargs=argparse.REMAINDER)
    p_demo.set_defaults(func=cmd_demo)

    from cli_tools import register as _register_tools  # noqa: WPS433 — keeps this file's import graph small
    _register_tools(sub)

    args = parser.parse_args(argv)
    if args.command is None:
        args.interval = 420
        args.dry_run = False
        return cmd_default(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
