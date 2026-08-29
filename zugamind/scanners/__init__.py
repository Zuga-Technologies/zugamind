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

from foundation.fs import atomic_write_text as _atomic_write_text
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
import logging as _logging
import time as _time

_logger = _logging.getLogger("zugamind.scanners")

_BYPASS_COOLDOWN_SEC = 3600


# Fields a scanner may use to declare a trigger's stable identity, in
# priority order. The list is not cosmetic: anything NOT here falls through to
# a hash of the detail TEXT, and a text hash is wrong in both directions at
# once — two different items with the same title collide into one key (the
# second is silently swallowed for the whole window), and editing an item's
# title mints a fresh key (it re-fires). ai_labs was fixed by adding "link";
# `post_slug`/`post_url` (reddit_ai) and `issue_id`/`issue_url`
# (github_issues) were still falling through on 2026-08-29 — measured: 6 of
# the 13 keys in the live state file were detail hashes.
_ID_FIELDS = ("story_id", "id", "issue_id", "post_slug", "url", "link",
              "post_url", "issue_url")


def _trigger_key(trigger: dict) -> str:
    """Stable identity for a trigger: an explicit id, else a hash of detail.

    Deliberately NOT prefixed with the producing field name. Doing so would
    fix a contrived cross-field collision (`{"id": "X"}` vs `{"url": "X"}`
    of one type) at the cost of invalidating every key in the live state file
    — a one-time re-fire of everything the agent has already damped. No live
    trigger type contains the delimiter; the trade is not worth it.
    """
    for field in _ID_FIELDS:
        value = trigger.get(field)
        # `is not None` rather than truthiness: a falsy-but-real id (0, "")
        # used to fall through to the detail hash and then collide with the
        # id-less trigger carrying the same text.
        if value is not None and value != "":
            return f"{trigger.get('type', '?')}:{value}"
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
    if default_window <= 0:
        # A zero/negative window silently turns habituation OFF and makes this
        # the cron job the README says it is not. One character in an env var
        # did it (ZUGAMIND_HABITUATION_HOURS=0), with no warning anywhere.
        _logger.warning(
            "habituation window is %.0fs (ZUGAMIND_HABITUATION_HOURS=%s) — "
            "repeat damping is OFF", default_window, _config.HABITUATION_HOURS,
        )

    fresh = []
    for t in triggers:
        # One malformed trigger used to raise mid-loop, which abandoned the
        # bookkeeping for every well-formed trigger already processed in the
        # batch — so the whole batch re-emitted every cycle until the bad one
        # went away. Skip the bad one; keep the batch.
        if not isinstance(t, dict):
            _logger.warning("habituation: skipping non-dict trigger %r", type(t).__name__)
            continue
        window = _BYPASS_COOLDOWN_SEC if t.get("bypass_habituation") else default_window
        key = _trigger_key(t)
        last = seen.get(key)
        if isinstance(last, (int, float)) and not isinstance(last, bool):
            age = now - last
            # `age >= 0` is the fix for a timestamp in the FUTURE. Without it
            # a negative age is trivially < window, so the trigger was damped
            # forever AND the prune below kept the poisoned entry forever —
            # the one branch where this function failed CLOSED while its own
            # docstring promised "never a fresh one being lost". A future
            # stamp is a clock artefact or another writer's units, never
            # legitimate (audit 2026-08-29).
            if age < 0:
                _logger.warning(
                    "habituation: %s has a last_seen %.0fs in the FUTURE — "
                    "treating as unseen and dropping the entry", key, -age,
                )
                seen.pop(key, None)
            elif age < window:
                continue  # seen recently — damped
        seen[key] = now
        fresh.append(t)

    # Prune anything older than the longest window so the file stays bounded,
    # and anything stamped in the future, which age-based eviction can never
    # reach.
    horizon = max(default_window, _BYPASS_COOLDOWN_SEC)
    seen = {k: ts for k, ts in seen.items()
            if isinstance(ts, (int, float)) and not isinstance(ts, bool)
            and 0 <= (now - ts) < horizon}
    try:
        _config.SEEN_TRIGGERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(_config.SEEN_TRIGGERS_FILE, _json.dumps(seen))
    except Exception as exc:  # noqa: BLE001 — best-effort, but never silent
        # This used to be a bare `pass`. An unwritable state file turns
        # habituation permanently off, and the agent then re-alerts on
        # everything forever with ZERO log records at any level — measured at
        # 12 of 12 cycles. The dynamic loader in this same file already
        # learned this ("Fail-OPEN, not fail-silent ... needs one line an
        # operator can find"); the writer never got the same treatment.
        _logger.warning(
            "habituation state not saved (%s) — repeat damping is OFF until "
            "this write succeeds", exc,
        )
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
        except Exception as e:  # noqa: BLE001
            # Fail-OPEN, not fail-silent: a broken dynamic scanner must not
            # break the cycle, but "my scanner never runs" needs one line an
            # operator can find. It used to log nothing at any level.
            _logger.warning("dynamic scanner %s failed to import (skipped): %s", module_name, e)
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
