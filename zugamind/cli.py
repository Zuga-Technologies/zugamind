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
                                 last wake, latest cycle. No watching.
    zugamind watch               attach to a running daemon and stream its
                                  activity live (real time, timestamped).
    zugamind demo                 run the zero-setup synthetic demo
                                   (python demo.py) — no daemon, no state.

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
ANSI = sys.stdout.isatty()
RESET = "\033[0m" if ANSI else ""
DIM = "\033[2m" if ANSI else ""
BOLD = "\033[1m" if ANSI else ""
GREEN = "\033[92m" if ANSI else ""
CYAN = "\033[96m" if ANSI else ""
RED = "\033[91m" if ANSI else ""
YELLOW = "\033[93m" if ANSI else ""


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


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
    if pid and _pid_alive(pid):
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
        subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True)
    else:
        os.kill(pid, signal.SIGTERM)
    PID_FILE.unlink(missing_ok=True)
    print(f"stopped (was PID {pid})")
    return 0


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

    if JOURNAL_FILE.exists():
        try:
            last_line = None
            with open(JOURNAL_FILE, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        last_line = line
            if last_line:
                ev = json.loads(last_line)
                print(f"last journal event: {ev.get('kind')} @ {ev.get('ts', '?')}")
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

    with open(JOURNAL_FILE, encoding="utf-8") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()

    try:
        while True:
            time.sleep(1)
            with open(JOURNAL_FILE, encoding="utf-8") as f:
                f.seek(pos)
                new = f.read()
                pos = f.tell()
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
                        _status_line(f"{DIM}[{_now()}]{RESET} cycle: {label} salience={sal_str}")
                    elif kind == "alarm":
                        print(f"\n\n{RED}{BOLD}! ALARM  [{_now()}]{RESET}  {ev.get('detail')}")
                    elif kind == "harness_invocation":
                        print(f"\n\n{GREEN}{BOLD}> WAKE FIRED  [{_now()}]{RESET}  "
                              f"harness={ev.get('harness')}")
                        stdout = (ev.get("stdout") or "").strip()
                        if stdout:
                            print(stdout)
                        print()
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
        cmd_start(args)
        print()
    return cmd_watch(args)


def main(argv: Optional[list[str]] = None) -> int:
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
    p_watch.set_defaults(func=cmd_watch)

    p_demo = sub.add_parser("demo", help="run the zero-setup synthetic demo")
    p_demo.add_argument("rest", nargs=argparse.REMAINDER)
    p_demo.set_defaults(func=cmd_demo)

    args = parser.parse_args(argv)
    if args.command is None:
        args.interval = 420
        args.dry_run = False
        return cmd_default(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
