"""Scanner package — the input layer of the cognitive cycle.

Each scanner watches one source (an HTTP feed, a file, a DB table, etc.) and
returns a list of trigger dicts. The cycle then runs all of them in
sequence, concatenates their output, runs habituation filtering, and
hands the result to the workspace bid pass.

A trigger dict has at minimum:
    type:       str  — what kind of event
    detail:     str  — short human-readable summary
    novelty:    float (0..1)
    relevance:  float (0..1)
    urgency:    float (0..1)
plus type-specific keys (url, story_id, lab, subreddit, …).

Re-exports the public scan_* surface so callers can do:
    from scanners import scan_hackernews
"""

from .world.hackernews import scan_hackernews
from .world.reddit_ai import scan_reddit_ai
from .world.ai_labs import scan_ai_labs

__all__ = ["scan_hackernews", "scan_reddit_ai", "scan_ai_labs",
           "discover_dynamic_scanners", "habituation_filter"]


# ---- Habituation filtering ----------------------------------------------------
#
# A trigger that already surfaced recently is damped — dropped from the cycle —
# until its window expires. This is the "notices once, then shuts up" behavior
# that separates the workspace from a cron job re-alerting on the same story
# forever. State is a small {trigger_key: last_seen_epoch} JSON file
# (config.SEEN_TRIGGERS_FILE); the window is config.HABITUATION_HOURS, except
# triggers marked `bypass_habituation: True` which re-emit on a 60-minute
# cooldown instead (for sources whose repeat IS the signal — see _template.py).
#
# Only the default world-scanners are habituated (stream.runner applies this
# per-scanner). Caller-injected `extra_scanners` bypass it by design: they are
# the caller's own synthetic sources — scripts/verify_harness.py re-plants its
# canary trigger every retry cycle and must not be damped.
import hashlib as _hashlib
import json as _json
import time as _time

_BYPASS_COOLDOWN_SEC = 3600


def _trigger_key(trigger: dict) -> str:
    """Stable identity for a trigger: prefer an explicit id, fall back to a
    hash of the detail text."""
    for k in ("story_id", "id", "url"):
        v = trigger.get(k)
        if v:
            return f"{trigger.get('type', '?')}:{v}"
    detail = str(trigger.get("detail", ""))
    digest = _hashlib.sha1(detail.encode("utf-8", "replace")).hexdigest()[:16]
    return f"{trigger.get('type', '?')}:{digest}"


def habituation_filter(triggers: list, now: "float | None" = None) -> list:
    """Drop triggers whose key was seen within its habituation window.

    Survivors are recorded as seen. Fail-silent throughout: a corrupt or
    unwritable seen-file must never sink the cycle — worst case is a repeat
    trigger getting through, never a fresh one being lost.

    `now` (epoch seconds) is injectable for tests; default is real time.
    """
    from foundation import config as _config  # lazy: scanners stay importable standalone

    if now is None:
        now = _time.time()

    try:
        seen = _json.loads(_config.SEEN_TRIGGERS_FILE.read_text())
        if not isinstance(seen, dict):
            seen = {}
    except Exception:
        seen = {}

    default_window = _config.HABITUATION_HOURS * 3600
    fresh = []
    for t in triggers:
        window = _BYPASS_COOLDOWN_SEC if t.get("bypass_habituation") else default_window
        key = _trigger_key(t)
        last = seen.get(key)
        if isinstance(last, (int, float)) and (now - last) < window:
            continue  # seen recently — damped
        seen[key] = now
        fresh.append(t)

    # Prune anything older than the longest window so the file stays bounded.
    horizon = max(default_window, _BYPASS_COOLDOWN_SEC)
    seen = {k: ts for k, ts in seen.items()
            if isinstance(ts, (int, float)) and (now - ts) < horizon}
    try:
        _config.SEEN_TRIGGERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _config.SEEN_TRIGGERS_FILE.write_text(_json.dumps(seen))
    except Exception:
        pass  # fail-silent: habituation state is best-effort
    return fresh


# ---- Dynamic scanner discovery -----------------------------------------------
#
# Scanners are normally registered via the static imports above. To allow a
# contributor to drop a new scanner file into scanners/ (or scanners/world/)
# and have it picked up without editing this __init__.py, we expose a
# discover_dynamic_scanners() helper. cognitive_stream calls it after the
# static .extend() calls so dynamically-found scanners run too.
#
# Contract: any module file in scanners/ whose name does not start with `_`
# and which exports a top-level callable starting with `scan_` is loaded.
# Names already statically imported above are skipped (de-dup).
import importlib as _importlib
import inspect as _inspect
import subprocess as _subprocess
from pathlib import Path as _Path


def _git_tracked_scanner_files(pkg_dir: "_Path"):
    """Return the set of absolute paths of git-COMMITTED .py files under pkg_dir,
    or None if git can't answer. Fail-closed safety: the agent must only
    auto-load scanner code that is committed — an uncommitted/injected scanner file
    must NOT execute live every cycle. None -> fail closed (load no dynamic scanner)."""
    try:
        out = _subprocess.run(
            ["git", "-C", str(pkg_dir), "ls-files", "--", "*.py"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
    except Exception:
        return None
    tracked = set()
    for line in out.splitlines():
        line = line.strip()
        if line:
            tracked.add((pkg_dir / line).resolve())
    return tracked


def discover_dynamic_scanners() -> dict:
    """Return {function_name: callable} for scanner modules not statically imported.

    Importing this function is cheap; calling it is moderately expensive
    (does a dir scan + N dynamic imports), so cognitive_stream calls it
    once at module-load time and caches the result.

    Only git-COMMITTED scanner files are loaded (fail-closed); an uncommitted
    file is skipped so dropped-in code never runs in the live cycle.
    """
    found: dict = {}
    statically_imported = set(__all__)
    pkg_dir = _Path(__file__).parent
    committed = _git_tracked_scanner_files(pkg_dir)
    if committed is None:
        # Cannot verify what is committed -> load NO dynamic scanner (fail closed).
        # The statically-imported spine scanners are unaffected.
        return found
    for path in sorted(pkg_dir.rglob("*.py")):  # rglob: scanners live in bucket subdirs (e.g. world/)
        if path.stem.startswith("_") or path.stem == "__init__":
            continue
        if path.resolve() not in committed:
            continue  # uncommitted/injected file — never auto-load it live
        rel = path.relative_to(pkg_dir).with_suffix("")
        # Exclude any `_`-prefixed DIRECTORY part (e.g. _drafts/, _quarantine/).
        # The stem skip above only guards files; rglob recurses into subdirs, so a
        # draft like _drafts/foo.py (stem `foo`) would otherwise load live. This is
        # the load-bearing shadow-first guard.
        if any(part.startswith("_") for part in rel.parts):
            continue
        module_name = f"{__name__}." + ".".join(rel.parts)
        try:
            mod = _importlib.import_module(module_name)
        except Exception:
            # Fail-silent: a broken dynamic scanner must not break the
            # cycle. cognitive_stream wraps each call in try/except too.
            continue
        for attr_name, attr in _inspect.getmembers(mod, _inspect.isfunction):
            if not attr_name.startswith("scan_"):
                continue
            if attr_name in statically_imported:
                continue
            if attr_name in found:
                continue
            found[attr_name] = attr
    return found
