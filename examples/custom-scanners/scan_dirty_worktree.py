"""Example private scanner — uncommitted work sitting too long.

Not part of the ZugaMind package; a worked example of the extra_scanners
pattern documented in examples/custom-scanners/README.md.

Watches a configured list of git worktrees for uncommitted changes that
have been sitting dirty past a threshold. This is a universal hazard for
any AI-coding-agent workflow: the agent writes and tests code, the session
ends, and nobody remembers to commit or verify it live before the next
session starts somewhere else.

"Dirty too long" is tracked with a small state file recording the first
time each repo was OBSERVED dirty — git itself has no notion of "when did
this become dirty," so this scanner approximates it by remembering the
first dirty sighting and clearing that timestamp the moment the repo goes
clean again (a repo that's dirty for 2 minutes while you're actively
working in it should never trigger; one still dirty a day later should).

A trigger's strength reflects WHAT is sitting there, not merely that
something is. This matters more than it looks: WorldSignalsModule scores an
external bid at 0.25 + 0.4*relevance + 0.2*urgency, so flat weights of
0.7/0.4 put every dirty repo at exactly 0.61 — above a typical calibrated
wake floor. Emitted flat, this scanner guarantees that ANY dirty repo buys a
real agent session, whether it is a half-finished feature or a lockfile that
`npm install` touched. Two conditions therefore drop a report below that
floor, where it still reaches the stream but is not worth waking for:

  1. nothing a person wrote is dirty — every changed path is a lockfile or a
     build artifact;
  2. nothing moved — the dirty set is identical to the one already reported a
     threshold ago, so the report would repeat something already ignored.

Configuration (env):
    ZUGAMIND_WATCH_WORKTREES     comma-separated absolute paths to git repos.
    ZUGAMIND_DIRTY_THRESHOLD_HOURS   how long a repo must stay continuously
                                     dirty before it triggers. Default 24.

Unset ZUGAMIND_WATCH_WORKTREES -> scanner is off, returns [].
Stdlib only (subprocess -> git status --porcelain). Fail-silent per scanner
contract.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("zugamind.examples.dirty_worktree")

_MAX_TRIGGERS = 5
_DATA_DIR = Path(os.environ.get("ZUGAMIND_DATA_DIR") or Path(__file__).resolve().parent / "data")
_CACHE_DIR = _DATA_DIR / "scanner_cache"
_STATE_FILE = _CACHE_DIR / "dirty_worktree_state.json"

# Paths whose contents a tool wrote, not a person. A dirty set made ONLY of
# these is churn, not carried work.
_GENERATED_SUFFIXES = (
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json",
    "poetry.lock", "Pipfile.lock", "uv.lock", "Cargo.lock", "composer.lock",
    "Gemfile.lock", "go.sum", "flake.lock",
)
_GENERATED_DIR_PARTS = ("node_modules/", "__pycache__/", "dist/", "build/", ".venv/")

# See the module docstring: with WorldSignals' 0.25 + 0.4*rel + 0.2*urg, these
# two pairs decide whether a signal costs a session or merely joins the stream.
#   strong -> 0.61   weak -> 0.43
_STRONG = {"novelty": 0.5, "relevance": 0.7, "urgency": 0.4}
_WEAK = {"novelty": 0.3, "relevance": 0.35, "urgency": 0.2}


def _watched_repos() -> list[str]:
    raw = os.environ.get("ZUGAMIND_WATCH_WORKTREES", "")
    return [r.strip() for r in raw.split(",") if r.strip()]


def _threshold_seconds() -> float:
    hours = float(os.environ.get("ZUGAMIND_DIRTY_THRESHOLD_HOURS", "24") or 24)
    return hours * 3600


def _is_generated(path: str) -> bool:
    """True for paths a tool writes and a person never edits by hand."""
    norm = path.replace("\\", "/")
    if norm.endswith(_GENERATED_SUFFIXES):
        return True
    return any(part in norm for part in _GENERATED_DIR_PARTS)


def _survey(repo_path: str) -> dict[str, Any] | None:
    """What is actually sitting uncommitted here, not merely whether something is.

    Returns None when the repo is clean.
    """
    try:
        status = subprocess.run(
            ["git", "-C", repo_path, "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
        lines = [line for line in status.stdout.splitlines() if line.strip()]
        if not lines:
            return None

        paths = [line[3:].strip().strip('"') for line in lines]
        authored = [pth for pth in paths if not _is_generated(pth)]

        # --shortstat covers tracked changes; untracked files contribute no
        # lines to it, but their mere existence is the hazard this scanner is
        # for, so they are counted by path via `authored` instead.
        churn = subprocess.run(
            ["git", "-C", repo_path, "diff", "--shortstat", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        stat = churn.stdout.strip()

        return {
            "files": len(paths),
            "authored_files": len(authored),
            "stat": stat,
            # The churn line is part of the fingerprint, so ongoing edits to the
            # same files still read as movement — only a genuinely frozen dirty
            # set fingerprints identically twice.
            "fingerprint": hashlib.sha1(
                ("|".join(sorted(paths)) + "||" + stat).encode("utf-8", "replace")
            ).hexdigest()[:16],
        }
    except Exception as e:
        logger.debug("git survey failed for %s: %s", repo_path, e)
        return None


def _load_state() -> dict[str, dict[str, Any]]:
    """Load state, upgrading the old bare-timestamp format in place.

    The first format was `{repo: first_seen_epoch}`; entries now also carry the
    fingerprint of the dirty set as last reported. An old file still loads — a
    bare timestamp becomes an entry with no fingerprint, which reads as "moved"
    on its first pass and so reports once at full strength.
    """
    try:
        if _STATE_FILE.exists():
            raw = json.loads(_STATE_FILE.read_text("utf-8"))
            return {
                key: (val if isinstance(val, dict) else {"first_seen": val})
                for key, val in raw.items()
            }
    except Exception:
        pass
    return {}


def _save_state(state: dict[str, dict[str, Any]]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(state), "utf-8")
    except Exception as e:
        logger.debug("dirty_worktree state save failed (non-fatal): %s", e)


def scan_dirty_worktree() -> list[dict[str, Any]]:
    """Return `dirty_worktree` triggers for repos dirty past the configured threshold."""
    repos = _watched_repos()
    if not repos:
        return []

    threshold = _threshold_seconds()
    now = time.time()
    state = _load_state()
    triggers: list[dict[str, Any]] = []
    candidates: list[tuple[dict[str, Any], str]] = []

    for repo in repos:
        survey = _survey(repo)
        entry = state.get(repo)

        if survey is None:
            if entry is not None:
                del state[repo]
            continue

        if entry is None:
            # Just went dirty — arm the clock, don't trigger. Deliberately no
            # fingerprint: the fingerprint records what was last REPORTED, and
            # stamping it here would make the first report read as "unchanged".
            state[repo] = {"first_seen": now}
            continue

        first_seen = entry.get("first_seen", now)
        age = now - first_seen
        if age < threshold:
            continue

        hours = age / 3600
        authored = survey["authored_files"] > 0
        moved = survey["fingerprint"] != entry.get("fingerprint")
        weights = _STRONG if (authored and moved) else _WEAK

        detail = f"{repo} has had uncommitted changes for {hours:.1f}h"
        if not authored:
            detail += " (generated files only)"
        elif not moved:
            detail += " (unchanged since last report)"

        candidates.append(({
            "type": "dirty_worktree",
            "detail": detail,
            **weights,
            "repo": repo,
            "dirty_hours": round(hours, 1),
            "dirty_files": survey["files"],
            "authored_files": survey["authored_files"],
            "churn": survey["stat"],
        }, survey["fingerprint"]))

    # _MAX_TRIGGERS caps how much this scanner may say per cycle. Spend it on
    # the strongest candidates rather than on whichever repo the config happened
    # to list first, or a lockfile can take the slot of a repo carrying a
    # thousand uncommitted lines.
    candidates.sort(key=lambda c: (-c[0]["relevance"], -c[0]["dirty_hours"]))
    for trigger, fingerprint in candidates[:_MAX_TRIGGERS]:
        triggers.append(trigger)
        # Reset the clock after reporting so this doesn't re-fire every single
        # cycle for the same still-dirty repo, and carry the fingerprint so the
        # next report can tell movement from stasis. A candidate cut by the cap
        # keeps its original first_seen and gets its turn next cycle.
        state[trigger["repo"]] = {"first_seen": now, "fingerprint": fingerprint}

    _save_state(state)
    return triggers


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for t in scan_dirty_worktree():
        print(t["detail"])
