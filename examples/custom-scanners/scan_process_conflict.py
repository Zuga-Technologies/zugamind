"""Example private scanner — don't let an agent touch files a human is live in.

Not part of the ZugaMind package; a worked example of the extra_scanners
pattern documented in examples/custom-scanners/README.md.

Watches a configured list of {label: process_name} pairs and emits a
trigger while any of them is currently running — the intended use is a
bid-modulator or a check your harness makes before an autonomous edit: "is
the human actively in the app this touches right now?" This project's own
overnight-build harness re-derives this exact rule by hand on almost every
autonomous wake ("found you actively in the game right now, touched
nothing") — repetition that consistent is the textbook case for automating
the check instead of re-deciding it every time.

This scanner only reports THAT a watched process is running, not a verdict
on whether to act — pair it with `wake_modules`/a bid modulator (see the
main README's "Architecture" section on `Workspace.register_modulator()`)
if you want it to actually suppress edits, rather than just surface as a
trigger.

Configuration (env):
    ZUGAMIND_PROCESS_WATCH   comma-separated "label:process_name" pairs,
                             e.g. "Ludus:LudusOverlay.exe,Editor:UnrealEditor.exe"
                             (POSIX process names don't need .exe).

Unset ZUGAMIND_PROCESS_WATCH -> scanner is off, returns [].
Stdlib only (subprocess -> tasklist on Windows, ps on POSIX). Fail-silent
per scanner contract. Cached 60s — process state changes fast but a
sub-cycle poll is wasted work.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("zugamind.examples.process_conflict")

_TTL = 60
_MAX_TRIGGERS = 5
_DATA_DIR = Path(os.environ.get("ZUGAMIND_DATA_DIR") or Path(__file__).resolve().parent / "data")
_CACHE_DIR = _DATA_DIR / "scanner_cache"
_CACHE_FILE = _CACHE_DIR / "process_conflict.json"


def _watch_pairs() -> list[tuple[str, str]]:
    raw = os.environ.get("ZUGAMIND_PROCESS_WATCH", "")
    pairs = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        label, proc = entry.split(":", 1)
        pairs.append((label.strip(), proc.strip()))
    return pairs


def _running_process_names() -> set[str]:
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["tasklist", "/fo", "csv", "/nh"],
                capture_output=True, text=True, timeout=10,
            )
            names = set()
            for line in result.stdout.splitlines():
                parts = line.split('","')
                if parts:
                    names.add(parts[0].strip('"').lower())
            return names
        else:
            result = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True, text=True, timeout=10)
            return {line.strip().lower() for line in result.stdout.splitlines() if line.strip()}
    except Exception as e:
        logger.debug("process list failed: %s", e)
        return set()


def _load_cache() -> dict[str, Any]:
    try:
        if _CACHE_FILE.exists():
            return json.loads(_CACHE_FILE.read_text("utf-8"))
    except Exception:
        pass
    return {"ts": 0, "running_labels": []}


def _save_cache(cache: dict[str, Any]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(cache), "utf-8")
    except Exception as e:
        logger.debug("process_conflict cache save failed (non-fatal): %s", e)


def scan_process_conflict() -> list[dict[str, Any]]:
    """Return `process_conflict` triggers for watched processes newly seen running."""
    pairs = _watch_pairs()
    if not pairs:
        return []

    cache = _load_cache()
    if time.time() - cache.get("ts", 0) < _TTL:
        return []

    running_names = _running_process_names()
    prev_labels = set(cache.get("running_labels", []))
    now_labels: list[str] = []
    triggers: list[dict[str, Any]] = []

    for label, proc in pairs:
        is_running = proc.lower() in running_names
        if is_running:
            now_labels.append(label)
            if label not in prev_labels and len(triggers) < _MAX_TRIGGERS:
                triggers.append({
                    "type": "process_conflict",
                    "detail": f"{label} ({proc}) is running — a human may be actively using it",
                    "novelty": 0.6,
                    "relevance": 0.7,
                    "urgency": 0.3,
                    "label": label,
                    "process": proc,
                })

    cache["ts"] = time.time()
    cache["running_labels"] = now_labels
    _save_cache(cache)
    return triggers


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for t in scan_process_conflict():
        print(t["detail"])
