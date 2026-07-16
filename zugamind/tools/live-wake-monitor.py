"""ZugaMind live wake monitor — real-time terminal visualization of the
engine's own attention cycle, for screen-recording or just watching live.

Shows a ticking, timestamped clock while idle (proves nothing is staged),
then timestamps the exact moment a real autonomous wake fires and the
moment its result lands — pulling straight from the engine's own journal,
no per-deployment config needed beyond locating the data dir.

Usage:
    pip install pyfiglet
    python tools/live-wake-monitor.py

Path resolution (same order as the zugamind-status skill):
    1. $ZUGAMIND_DATA_DIR env var, if set — journal/data live there directly.
    2. ./data/engine/journal.jsonl relative to cwd.
    3. ./zugamind/data/engine/journal.jsonl (cwd is a parent project).
    4. Otherwise: error out with the paths it tried.

Optional: $ZUGAMIND_RESULT_FILE — if your harness writes its result to a
separate file (e.g. an observation-mode harness appending to a notes file)
rather than relying on the journal's own captured stdout, point this at
it and the monitor will tail that file for the result instead.
"""
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime

import pyfiglet

RESET = "\033[0m"
DIM = "\033[2m"
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BOLD = "\033[1m"
BOLDTXT = "\033[1;97m"
CODE = "\033[93m"
CITATION = "\033[90m"
BULLET = "\033[96m"
HEADING = "\033[1;96m"

WRAP_WIDTH = max(20, shutil.get_terminal_size(fallback=(70, 24)).columns - 2)
ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def find_journal():
    env = os.environ.get("ZUGAMIND_DATA_DIR")
    candidates = []
    if env:
        candidates.append(os.path.join(env, "journal.jsonl"))
    candidates.append(os.path.join("data", "engine", "journal.jsonl"))
    candidates.append(os.path.join("zugamind", "data", "engine", "journal.jsonl"))
    for c in candidates:
        if os.path.exists(c):
            return c
    sys.exit(
        "Could not find journal.jsonl. Tried:\n  "
        + "\n  ".join(candidates)
        + "\n\nSet $ZUGAMIND_DATA_DIR to your deployment's data/engine dir, "
        "or run this from the package root / its parent project."
    )


def now_str():
    return datetime.now().strftime("%H:%M:%S")


def clock_line(status):
    sys.stdout.write(f"\r{DIM}[{now_str()}]{RESET} {status}" + " " * 15)
    sys.stdout.flush()


def visible_len(s):
    return len(ANSI_RE.sub("", s))


def wrap_ansi(text, width, indent="", first_indent=""):
    words = [w for w in text.split(" ") if w]
    if not words:
        return ""
    lines, prefix, cur = [], first_indent, []
    for w in words:
        trial = prefix + " ".join(cur + [w])
        if cur and visible_len(trial) > width:
            lines.append(prefix + " ".join(cur))
            prefix, cur = indent, [w]
        else:
            cur.append(w)
    lines.append(prefix + " ".join(cur))
    return "\n".join(lines)


def style_inline(text):
    text = re.sub(r"\*\*(.+?)\*\*", f"{BOLDTXT}\\1{RESET}", text)
    text = re.sub(r"`([^`]+)`", f"{CODE}\\1{RESET}", text)
    text = re.sub(r"\((\d[\d,\s\-–]*)\)", f"{CITATION}(\\1){RESET}", text)
    text = text.replace("✓", f"{GREEN}✓{RESET}")
    return text


def render_block(text):
    out = []
    for raw in text.split("\n"):
        if not raw.strip():
            out.append("")
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", raw)
        if m:
            out.append(wrap_ansi(f"{HEADING}{m.group(2)}{RESET}", WRAP_WIDTH, "  ", f"{HEADING}▌{RESET} "))
            continue
        m = re.match(r"^(\s*)[-*]\s+(.*)$", raw)
        if m:
            out.append(wrap_ansi(style_inline(m.group(2)), WRAP_WIDTH, "  ", f"{BULLET}›{RESET} "))
            continue
        out.append(wrap_ansi(style_inline(raw), WRAP_WIDTH))
    return "\n".join(out)


def main():
    journal = find_journal()
    result_file = os.environ.get("ZUGAMIND_RESULT_FILE")

    banner = pyfiglet.figlet_format("ZUGAMIND", font="small", width=WRAP_WIDTH + 4)
    print(f"{GREEN}{banner}{RESET}")
    print(f"{DIM}{CODE}live wake monitor — nothing below is staged{RESET}")
    print(f"{DIM}watching {journal} in real time{RESET}")
    print(f"{DIM}started {now_str()}{RESET}\n")

    with open(journal, encoding="utf-8") as f:
        f.seek(0, os.SEEK_END)
        jpos = f.tell()

    rpos = None
    if result_file and os.path.exists(result_file):
        with open(result_file, encoding="utf-8") as f:
            f.seek(0, os.SEEK_END)
            rpos = f.tell()

    stdout_result = None
    while stdout_result is None:
        time.sleep(1)
        with open(journal, encoding="utf-8") as f:
            f.seek(jpos)
            new = f.read()
            jpos = f.tell()
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
                    w = ev.get("winner", {})
                    clock_line(f"cycle: {w.get('source_module', '?')} salience={w.get('salience', 0):.3f}")
                elif kind == "alarm":
                    print(f"\n\n{RED}{BOLD}⚠ ALARM  [{now_str()}]{RESET}  {ev.get('detail')}")
                elif kind == "harness_invocation":
                    print(f"\n\n{GREEN}{BOLD}✓ WAKE FIRED — UNPROMPTED  [{now_str()}]{RESET}")
                    print(f"{DIM}harness: {ev.get('harness')}   event ts: {ev.get('ts', '')}{RESET}\n")
                    stdout_result = ev.get("stdout", "")
        else:
            clock_line("watching for the next real cycle...")

    if stdout_result and not result_file:
        print(f"{GREEN}{BOLD}✓ RESULT  [{now_str()}]{RESET}\n")
        print(render_block(stdout_result.strip()))
        return

    print(f"{YELLOW}waiting for the result to land in {result_file}...{RESET}\n")
    while True:
        time.sleep(1)
        with open(result_file, encoding="utf-8") as f:
            f.seek(rpos)
            new = f.read()
            rpos = f.tell()
        if new.strip():
            print(f"{GREEN}{BOLD}✓ RESULT LANDED  [{now_str()}]{RESET}\n")
            print(render_block(new.strip()))
            break
        clock_line(f"watching {os.path.basename(result_file)}...")


if __name__ == "__main__":
    main()
