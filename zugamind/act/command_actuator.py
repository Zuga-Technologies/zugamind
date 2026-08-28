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
A malformed entry — a non-integer cap or timeout, an `enabled` that is not
a boolean (`"false"` the string used to ENABLE a harness, because
bool("false") is True), a `wake_min_salience` that is neither a number
nor "calibrate" (a typo used to mean NO floor: wake on everything), or a
duplicate name — is skipped with a warning, never loaded half-right
(audit 2026-08-28). Skipping fails closed: a harness you cannot describe
correctly does not wake at all until you fix the line.

Dry runs journal exactly what WOULD run but do not consume the rate-limit
quota — a `--dry-run` preview against a shared journal must not starve the
real daemon of its wakes. On timeout the WHOLE process tree is killed:
the .cmd shims Node CLIs install run through `cmd.exe /c`, and killing
cmd.exe alone left the real harness running — and holding the stdout
pipe, so the wake blocked until the orphan finished (measured 2026-08-28:
a 2s timeout returned after 6.1s with the child still alive).

The same file may also carry a top-level "quiet_hours" block —
`{"harnesses": [...], "quiet_hours": {"start": "23:00", "end": "07:00"}}` —
read by `load_quiet_hours()`. `ZUGAMIND_QUIET_HOURS` ("HH:MM-HH:MM") is an
env override that wins over the file. During quiet hours the STREAM RUNNER
(not this module) suppresses harness invocations and journals
"quiet_hours_deferred" instead — perception and journaling never stop, only
the wake call does. See `stream/runner.py`.

Stdlib-only (json + os + shutil + signal + subprocess + sys + tempfile).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from continuity import journal
from foundation.config import DATA_DIR
from foundation.failure_reason import map_local_slug

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
# After a timeout kills the process tree, how long to wait for the pipes to
# drain before giving up on the harness's output. A survivor that escaped
# the tree kill would otherwise hold the pipe open and block the mind.
_TREE_KILL_GRACE_SEC = 10


def _resolve_windows_shim(argv: List[Any]) -> List[Any]:
    """Route .cmd/.bat shims through cmd.exe on Windows.

    Node/npm-installed CLIs (codex, openclaw, ...) put a .cmd batch wrapper
    on PATH, not a real .exe. subprocess.run without shell=True calls
    CreateProcess directly, which cannot launch a batch file and fails with
    WinError 2 ("system cannot find the file") even though the file is
    right there on PATH. Only .cmd/.bat need this — a real .exe (e.g.
    claude.exe) runs fine as-is. Kept as an explicit argv prefix rather than
    shell=True so per-argument quoting (the briefing file path) stays
    subprocess-safe instead of shell-parsed.
    """
    if sys.platform != "win32" or not argv:
        return argv
    resolved = shutil.which(str(argv[0]))
    if resolved and resolved.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", *argv]
    return argv


def _as_bool(value: Any, default: bool = True) -> bool:
    """A real boolean, or the obvious spellings of one. Anything else raises —
    bool("false") is True, and that once silently ENABLED a harness."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off"):
            return False
    raise ValueError(f"not a boolean: {value!r}")


def _cap(config: Dict[str, Any], key: str, default: int) -> int:
    """An integer config value, or the module default with a warning. Used on
    the hand-built dicts invoke_harness accepts directly; the loader rejects
    bad values outright. Never raises."""
    raw = config.get(key, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("harness %r: %s=%r is not an integer; using default %d",
                       config.get("name"), key, raw, default)
        return default


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
    seen_names: set = set()
    for entry in raw:
        if not isinstance(entry, dict) or "name" not in entry or "command" not in entry:
            logger.warning("skipping malformed harness config entry: %r", entry)
            continue
        command = entry.get("command")
        if not isinstance(command, list):
            logger.warning("skipping harness %r — command is not a list", entry.get("name"))
            continue
        name = str(entry["name"])
        if name in seen_names:
            # Two entries with one name would share one rate-limit counter and
            # both fire on every wake; the file is wrong, not ambiguous.
            logger.warning("skipping duplicate harness name %r (first entry wins)", name)
            continue
        try:
            cfg = {
                "name": name,
                "command": list(command),
                "timeout_sec": int(entry.get("timeout_sec", _DEFAULT_TIMEOUT_SEC)),
                "max_per_hour": int(entry.get("max_per_hour", _DEFAULT_MAX_PER_HOUR)),
                "max_per_day": int(entry.get("max_per_day", _DEFAULT_MAX_PER_DAY)),
                "enabled": _as_bool(entry.get("enabled"), True),
            }
            # Optional wake filters (consumed by stream.runner). Rehearsal lesson:
            # this normalizer once dropped unknown keys, silently disabling the
            # filter that the config visibly declared — keep it explicit.
            wake_modules = entry.get("wake_modules")
            if isinstance(wake_modules, list) and wake_modules:
                cfg["wake_modules"] = [str(m) for m in wake_modules]
            floor = entry.get("wake_min_salience")
            if floor is None:
                pass
            elif isinstance(floor, (int, float)) and not isinstance(floor, bool):
                cfg["wake_min_salience"] = float(floor)
            elif floor == "calibrate":
                # Opt-in self-calibrating floor (issue #12) — see act/floor_calibration.py.
                cfg["wake_min_salience"] = "calibrate"
            else:
                # A typo here ("calibrated", "0.6" the string, ...) used to be
                # dropped silently, which meant NO floor — the harness woke on
                # every winner, the opposite of what the file visibly declared.
                raise ValueError(f"wake_min_salience={floor!r} is neither a number nor \"calibrate\"")
        except (TypeError, ValueError) as e:
            logger.warning("skipping harness %r — %s", name, e)
            continue
        seen_names.add(name)
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


def _consumes_quota(event: Dict[str, Any], name: str) -> bool:
    """Which journal events count against a harness's rate limit: real
    invocations only. Dry runs are journaled (they are the audit trail of what
    WOULD have run) but a `--dry-run` preview against a shared journal must
    not starve the real daemon; `empty_command` never ran anything."""
    return (event.get("kind") == "harness_invocation"
            and event.get("harness") == name
            and not event.get("dry_run")
            and event.get("error") != "empty_command")


def _recent_invocation_counts(name: str, now: Optional[float] = None) -> Optional[tuple]:
    """(last hour, last 24h) quota-consuming invocations of `name`, from ONE
    journal read.

    Returns None when the journal file exists but cannot be read. The rate
    limiter is one of the few hard safety controls on the wake path, so an
    unknowable count must refuse the invocation (fail closed) rather than
    silently reset to zero — the caller treats None as "over the limit".
    A missing journal (fresh install) legitimately counts as 0.
    """
    t = now if now is not None else time.time()
    day_cutoff = datetime.fromtimestamp(t - _RATE_WINDOW_DAY_SEC, tz=timezone.utc).isoformat()
    hour_cutoff = datetime.fromtimestamp(t - _RATE_WINDOW_HOUR_SEC, tz=timezone.utc).isoformat()
    try:
        events = journal.read_events(since_iso=day_cutoff, limit=5000, on_error="raise")
    except Exception as e:  # noqa: BLE001 — unknowable count means refuse, not zero
        logger.error("rate-limit count unavailable for %r (journal unreadable): %s", name, e)
        return None
    day = [e for e in events if _consumes_quota(e, name)]
    hour = [e for e in day if e.get("ts", "") > hour_cutoff]
    return len(hour), len(day)


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


def _kill_tree(proc: "subprocess.Popen") -> None:
    """Kill `proc` AND everything it spawned. On Windows the .cmd shims Node
    CLIs install run through `cmd.exe /c`, so the harness is a grandchild
    and `proc.kill()` alone leaves it running (and holding our stdout pipe);
    `taskkill /T` walks the tree. On POSIX the child was started in its own
    session, so the process group is the tree."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=30)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception as e:  # noqa: BLE001 — fall back to the direct child
        logger.warning("tree kill failed for pid %s (%s); killing the direct child only", proc.pid, e)
    try:
        proc.kill()
    except Exception:  # noqa: BLE001 — already gone
        pass


def _run_harness(argv: List[Any], timeout_sec: int) -> tuple:
    """Run `argv` to completion or timeout. Returns (returncode, stdout,
    stderr, timed_out). Output is decoded as UTF-8 with replacement: every
    supported harness emits UTF-8, and the locale codec (cp1252 on a Windows
    box without PYTHONUTF8) turned em-dashes and emoji into mojibake or a
    decode exception that lost the whole wake's output. On timeout the whole
    tree is killed first, then the pipes are drained for up to
    _TREE_KILL_GRACE_SEC so partial output is salvaged without letting a
    survivor block the mind."""
    kwargs: Dict[str, Any] = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8", errors="replace")
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(argv, **kwargs)
    try:
        out, err = proc.communicate(timeout=timeout_sec)
        return proc.returncode, out or "", err or "", False
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=_TREE_KILL_GRACE_SEC)
        except subprocess.TimeoutExpired:
            out, err = "", ""  # a survivor still holds the pipe; the wake must not block on it
        return None, out or "", err or "", True


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

    max_per_hour = _cap(config, "max_per_hour", _DEFAULT_MAX_PER_HOUR)
    max_per_day = _cap(config, "max_per_day", _DEFAULT_MAX_PER_DAY)
    counts = _recent_invocation_counts(name)
    if counts is None:
        # Journal unreadable -> the caps can't be checked. Refusing is the
        # only answer consistent with the rest of this codebase's fail-closed
        # posture; treating it as zero would erase both rate limits exactly
        # when the audit trail is already broken.
        journal.append_event("harness_rate_limit_indeterminate", {"harness": name})
        return {
            "ok": False, "error": "rate_limit_indeterminate", "harness": name,
            "failure_reason": map_local_slug("rate_limit_indeterminate"),
        }
    hour_count, day_count = counts
    if hour_count >= max_per_hour:
        journal.append_event("harness_rate_limited", {
            "harness": name, "window": "hour",
            "max_per_hour": max_per_hour, "recent_count": hour_count,
        })
        return {
            "ok": False, "error": "rate_limited", "harness": name, "window": "hour",
            "max_per_hour": max_per_hour, "recent_count": hour_count,
            "failure_reason": map_local_slug(
                f"rate_limited (hour cap: {hour_count}/{max_per_hour})"
            ),
        }
    if day_count >= max_per_day:
        journal.append_event("harness_rate_limited", {
            "harness": name, "window": "day",
            "max_per_day": max_per_day, "recent_count": day_count,
        })
        return {
            "ok": False, "error": "rate_limited", "harness": name, "window": "day",
            "max_per_day": max_per_day, "recent_count": day_count,
            "failure_reason": map_local_slug(
                f"rate_limited (day cap: {day_count}/{max_per_day})"
            ),
        }

    command = config.get("command") or []
    if not command:
        result = {
            "ok": False, "error": "empty_command", "harness": name, "dry_run": dry_run,
            "failure_reason": map_local_slug("empty_command"),
        }
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
        argv = _resolve_windows_shim(argv)

        if dry_run:
            result = {"ok": True, "harness": name, "dry_run": True, "would_run": argv}
        else:
            timeout_sec = _cap(config, "timeout_sec", _DEFAULT_TIMEOUT_SEC)
            try:
                rc, out, err, timed_out = _run_harness(argv, timeout_sec)
                if timed_out:
                    # Whatever the harness produced before the kill is kept:
                    # dropping it made a timed-out wake indistinguishable from
                    # a wake that ran and said nothing (2026-08-16: first live
                    # wake timed out and journaled an empty result).
                    result = {
                        "ok": False, "error": "timeout", "harness": name,
                        "dry_run": False, "timeout_sec": timeout_sec, "killed_tree": True,
                        "failure_reason": map_local_slug("timeout"),
                        "stdout": out[:_STDOUT_STDERR_CAP],
                        "stderr": err[:_STDOUT_STDERR_CAP],
                    }
                else:
                    result = {
                        "ok": rc == 0,
                        "harness": name,
                        "dry_run": False,
                        "returncode": rc,
                        "stdout": out[:_STDOUT_STDERR_CAP],
                        "stderr": err[:_STDOUT_STDERR_CAP],
                    }
            except Exception as e:  # noqa: BLE001 — never raise out of invoke_harness
                error = f"invoke_error:{e}"
                result = {
                    "ok": False, "error": error, "harness": name, "dry_run": False,
                    "failure_reason": map_local_slug(error),
                }
    except Exception as e:  # noqa: BLE001 — covers temp-file / substitution failures too
        error = f"setup_error:{e}"
        result = {
            "ok": False, "error": error, "harness": name, "dry_run": dry_run,
            "failure_reason": map_local_slug(error),
        }
    finally:
        if briefing_path:
            try:
                os.unlink(briefing_path)
            except OSError as e:
                # A briefing can carry workspace content; a leftover is worth a line.
                logger.warning("briefing file not removed (%s): %s", briefing_path, e)

    journal.append_event("harness_invocation", result)
    return result


__all__ = ["invoke_harness", "load_harness_configs", "load_quiet_hours", "DEFAULT_HARNESS_CONFIG"]
