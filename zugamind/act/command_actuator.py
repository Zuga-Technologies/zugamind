"""ZugaMind command actuator — the harness adapter.

Turns an already-approved workspace decision into a real subprocess
invocation of the user's agent harness (Claude Code, OpenClaw, Hermes, a
generic webhook via curl, ...). This module ONLY executes; it does not
decide whether to. It never raises.

CONTRACT — callers MUST pass every invocation through the fail-closed
action gate first:

    intent = {...}
    gate_result = gates.action_gate.escalate_for_action(intent, dry_run=...)
    if gate_result["ok"]:
        for hc in load_harness_configs():
            if hc["enabled"]:
                invoke_harness(hc, briefing, dry_run=...)

`zugamind.stream.runner` is the reference caller and shows the full order:
workspace winner -> WorkspacePlanner -> continuity.journal.build_briefing ->
gates.action_gate.escalate_for_action -> (only if approved) invoke_harness.
This module does not re-check the gate itself — skipping the gate at the
call site is a caller bug, not something this module can detect.

Harness configs are loaded from a JSON file: a list of objects shaped like

    {"name": str, "command": [argv...], "timeout_sec": int, "max_per_hour":
     int, "max_per_day": int, "enabled": bool}

`command` is an argv list; the literal substring "{briefing_file}" in any
argv element is replaced with the path to a temp file containing the
briefing text. See `examples/harness-configs/` for worked examples.

The same file may also carry a top-level "quiet_hours" block —
`{"harnesses": [...], "quiet_hours": {"start": "23:00", "end": "07:00"}}` —
read by `load_quiet_hours()`. `ZUGAMIND_QUIET_HOURS` ("HH:MM-HH:MM") is an
env override that wins over the file. During quiet hours the STREAM RUNNER
(not this module) suppresses harness invocations and journals
"quiet_hours_deferred" instead — perception and journaling never stop, only
the wake call does. See `stream/runner.py`.

Stdlib-only (json + os + subprocess + tempfile).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from continuity import journal
from foundation.config import DATA_DIR

logger = logging.getLogger("zugamind.act.command_actuator")

# Default location of the harness config file, overridable via
# ZUGAMIND_HARNESS_CONFIG. Lives under the gitignored data dir (see
# foundation/config.py) — this is runtime configuration, not source.
DEFAULT_HARNESS_CONFIG: Path = DATA_DIR / "harness.json"

_DEFAULT_TIMEOUT_SEC = 300
_DEFAULT_MAX_PER_HOUR = 4
_DEFAULT_MAX_PER_DAY = 20
_STDOUT_STDERR_CAP = 2000
_RATE_WINDOW_HOUR_SEC = 3600
_RATE_WINDOW_DAY_SEC = 24 * 3600


def _config_path() -> Path:
    override = os.environ.get("ZUGAMIND_HARNESS_CONFIG")
    return Path(override) if override else DEFAULT_HARNESS_CONFIG


def load_harness_configs(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Load + normalize harness configs from JSON.

    Accepts either a bare JSON list of config objects or `{"harnesses":
    [...]}`. A missing file returns []. A malformed file, or an entry
    missing "name"/"command", is skipped (logged), never raised.
    """
    p = path or _config_path()
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 — a bad config file must not crash the caller
        logger.warning("harness config load failed (non-fatal): %s", e)
        return []

    if isinstance(raw, dict):
        raw = raw.get("harnesses", [])
    if not isinstance(raw, list):
        logger.warning("harness config at %s is not a list (ignoring)", p)
        return []

    configs: List[Dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict) or "name" not in entry or "command" not in entry:
            logger.warning("skipping malformed harness config entry: %r", entry)
            continue
        command = entry.get("command")
        if not isinstance(command, list):
            logger.warning("skipping harness %r — command is not a list", entry.get("name"))
            continue
        cfg = {
            "name": str(entry["name"]),
            "command": list(command),
            "timeout_sec": int(entry.get("timeout_sec", _DEFAULT_TIMEOUT_SEC)),
            "max_per_hour": int(entry.get("max_per_hour", _DEFAULT_MAX_PER_HOUR)),
            "max_per_day": int(entry.get("max_per_day", _DEFAULT_MAX_PER_DAY)),
            "enabled": bool(entry.get("enabled", True)),
        }
        # Optional wake filters (consumed by stream.runner). Rehearsal lesson:
        # this normalizer once dropped unknown keys, silently disabling the
        # filter that the config visibly declared — keep it explicit.
        wake_modules = entry.get("wake_modules")
        if isinstance(wake_modules, list) and wake_modules:
            cfg["wake_modules"] = [str(m) for m in wake_modules]
        floor = entry.get("wake_min_salience")
        if isinstance(floor, (int, float)):
            cfg["wake_min_salience"] = float(floor)
        configs.append(cfg)
    return configs


def _parse_hhmm_range(value: str) -> Optional[Dict[str, str]]:
    """Parse "HH:MM-HH:MM" into {"start": "HH:MM", "end": "HH:MM"}, or None
    if malformed (never raises)."""
    try:
        start_s, end_s = value.split("-", 1)
        start_s, end_s = start_s.strip(), end_s.strip()
        for hhmm in (start_s, end_s):
            h, m = hhmm.split(":")
            if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
                return None
        return {"start": start_s, "end": end_s}
    except Exception:
        return None


def load_quiet_hours(path: Optional[Path] = None) -> Optional[Dict[str, str]]:
    """Return the configured quiet-hours window as {"start": "HH:MM", "end":
    "HH:MM"}, or None if none is configured.

    Resolution order: the `ZUGAMIND_QUIET_HOURS` env var ("HH:MM-HH:MM")
    wins if set and well-formed; otherwise a top-level "quiet_hours" block
    in the harness config file itself (only present when that file uses the
    `{"harnesses": [...], "quiet_hours": {...}}` dict form — a bare list has
    no top-level slot for it). Malformed values are ignored (treated as "no
    quiet hours configured"), never raised.
    """
    env_val = os.environ.get("ZUGAMIND_QUIET_HOURS")
    if env_val:
        parsed = _parse_hhmm_range(env_val)
        if parsed:
            return parsed
        logger.warning("ZUGAMIND_QUIET_HOURS=%r is malformed (want HH:MM-HH:MM); ignoring", env_val)

    p = path or _config_path()
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    quiet = raw.get("quiet_hours")
    if not isinstance(quiet, dict) or "start" not in quiet or "end" not in quiet:
        return None
    return {"start": str(quiet["start"]), "end": str(quiet["end"])}


def _recent_invocation_count(name: str, window_sec: int, now: Optional[float] = None) -> Optional[int]:
    """Count "harness_invocation" journal events for `name` in the last `window_sec`.

    Returns None when the journal file exists but cannot be read. The rate
    limiter is one of the few hard safety controls on the wake path, so an
    unknowable count must refuse the invocation (fail closed) rather than
    silently reset to zero — the caller treats None as "over the limit".
    A missing journal (fresh install) legitimately counts as 0.
    """
    t = now if now is not None else time.time()
    cutoff_iso = datetime.fromtimestamp(t - window_sec, tz=timezone.utc).isoformat()
    try:
        events = journal.read_events(since_iso=cutoff_iso, limit=2000, on_error="raise")
    except Exception as e:  # noqa: BLE001 — unknowable count means refuse, not zero
        logger.error("rate-limit count unavailable for %r (journal unreadable): %s", name, e)
        return None
    return sum(
        1 for e in events
        if e.get("kind") == "harness_invocation" and e.get("harness") == name
    )


def _briefing_dir() -> Optional[str]:
    """Directory briefing files are written to (created if missing).

    Defaults to DATA_DIR/briefings — inside the package data directory —
    rather than the system temp dir, because sandboxed harnesses (notably
    Claude Code's non-interactive `-p` mode) refuse to read files outside
    their working directory, and a briefing the harness cannot read
    silently wastes the wake. Override with ZUGAMIND_BRIEFING_DIR; returns
    None (system temp dir) if the directory cannot be created.
    """
    root = os.environ.get("ZUGAMIND_BRIEFING_DIR") or str(DATA_DIR / "briefings")
    try:
        os.makedirs(root, exist_ok=True)
        return root
    except OSError:
        return None


def invoke_harness(config: Dict[str, Any], briefing: str, dry_run: bool = False) -> Dict[str, Any]:
    """Invoke one harness with a wake briefing. Never raises.

    Writes `briefing` to a temp file, substitutes the literal
    "{briefing_file}" placeholder in every string argv element with that
    file's path, then runs the command (or, if `dry_run`, just journals
    what WOULD run). Always journals a "harness_invocation" event with the
    outcome. Rate-limited on TWO independent windows, both counted from the
    journal itself (never an in-memory counter, so limits survive a
    restart): `config["max_per_hour"]` per rolling hour and
    `config["max_per_day"]` per rolling 24h. Either cap being hit journals a
    "harness_rate_limited" event (noting which window) and refuses the call
    — the per-day cap exists specifically so a per-hour-compliant harness
    can't still be woken dozens of times across a full day.

    Returns a dict with at least `ok: bool` and `harness: str`; on any
    failure also `error: str` describing what went wrong (bad command,
    timeout, OS error, rate limit, ...) — the caller should never need to
    catch an exception from this function.
    """
    name = config.get("name", "unknown")

    if not config.get("enabled", True):
        return {"ok": False, "error": "harness_disabled", "harness": name}

    max_per_hour = int(config.get("max_per_hour", _DEFAULT_MAX_PER_HOUR))
    hour_count = _recent_invocation_count(name, _RATE_WINDOW_HOUR_SEC)
    if hour_count is None:
        # Journal unreadable -> the caps can't be checked. Refusing is the
        # only answer consistent with the rest of this codebase's fail-closed
        # posture; treating it as zero would erase both rate limits exactly
        # when the audit trail is already broken.
        journal.append_event("harness_rate_limit_indeterminate", {"harness": name})
        return {"ok": False, "error": "rate_limit_indeterminate", "harness": name}
    if hour_count >= max_per_hour:
        journal.append_event("harness_rate_limited", {
            "harness": name, "window": "hour",
            "max_per_hour": max_per_hour, "recent_count": hour_count,
        })
        return {
            "ok": False, "error": "rate_limited", "harness": name, "window": "hour",
            "max_per_hour": max_per_hour, "recent_count": hour_count,
        }

    max_per_day = int(config.get("max_per_day", _DEFAULT_MAX_PER_DAY))
    day_count = _recent_invocation_count(name, _RATE_WINDOW_DAY_SEC)
    if day_count is None:
        journal.append_event("harness_rate_limit_indeterminate", {"harness": name})
        return {"ok": False, "error": "rate_limit_indeterminate", "harness": name}
    if day_count >= max_per_day:
        journal.append_event("harness_rate_limited", {
            "harness": name, "window": "day",
            "max_per_day": max_per_day, "recent_count": day_count,
        })
        return {
            "ok": False, "error": "rate_limited", "harness": name, "window": "day",
            "max_per_day": max_per_day, "recent_count": day_count,
        }

    command = config.get("command") or []
    if not command:
        result = {"ok": False, "error": "empty_command", "harness": name, "dry_run": dry_run}
        journal.append_event("harness_invocation", result)
        return result

    briefing_path: Optional[str] = None
    result: Dict[str, Any]
    try:
        fd, briefing_path = tempfile.mkstemp(
            prefix="zugamind_briefing_", suffix=".md", dir=_briefing_dir()
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(briefing)

        argv = [
            arg.replace("{briefing_file}", briefing_path) if isinstance(arg, str) else arg
            for arg in command
        ]

        if dry_run:
            result = {"ok": True, "harness": name, "dry_run": True, "would_run": argv}
        else:
            timeout_sec = int(config.get("timeout_sec", _DEFAULT_TIMEOUT_SEC))
            try:
                proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_sec)
                result = {
                    "ok": proc.returncode == 0,
                    "harness": name,
                    "dry_run": False,
                    "returncode": proc.returncode,
                    "stdout": (proc.stdout or "")[:_STDOUT_STDERR_CAP],
                    "stderr": (proc.stderr or "")[:_STDOUT_STDERR_CAP],
                }
            except subprocess.TimeoutExpired:
                result = {
                    "ok": False, "error": "timeout", "harness": name,
                    "dry_run": False, "timeout_sec": timeout_sec,
                }
            except Exception as e:  # noqa: BLE001 — never raise out of invoke_harness
                result = {"ok": False, "error": f"invoke_error:{e}", "harness": name, "dry_run": False}
    except Exception as e:  # noqa: BLE001 — covers temp-file / substitution failures too
        result = {"ok": False, "error": f"setup_error:{e}", "harness": name, "dry_run": dry_run}
    finally:
        if briefing_path:
            try:
                os.unlink(briefing_path)
            except OSError:
                pass

    journal.append_event("harness_invocation", result)
    return result


__all__ = ["invoke_harness", "load_harness_configs", "load_quiet_hours", "DEFAULT_HARNESS_CONFIG"]
