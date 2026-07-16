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

Configuration (env):
    ZUGAMIND_WATCH_WORKTREES     comma-separated absolute paths to git repos.
    ZUGAMIND_DIRTY_THRESHOLD_HOURS   how long a repo must stay continuously
                                     dirty before it triggers. Default 24.

Unset ZUGAMIND_WATCH_WORKTREES -> scanner is off, returns [].
Stdlib only (subprocess -> git status --porcelain). Fail-silent per scanner
contract.
"""

from __future__ import annotations

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


def _watched_repos() -> list[str]:
    raw = os.environ.get("ZUGAMIND_WATCH_WORKTREES", "")
    return [r.strip() for r in raw.split(",") if r.strip()]


def _threshold_seconds() -> float:
    hours = float(os.environ.get("ZUGAMIND_DIRTY_THRESHOLD_HOURS", "24") or 24)
    return hours * 3600


def _is_dirty(repo_path: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
        return bool(result.stdout.strip())
    except Exception as e:
        logger.debug("git status failed for %s: %s", repo_path, e)
        return False


def _load_state() -> dict[str, float]:
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text("utf-8"))
    except Exception:
        pass
    return {}


def _save_state(state: dict[str, float]) -> None:
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

    for repo in repos:
        dirty = _is_dirty(repo)
        first_seen = state.get(repo)

        if not dirty:
            if first_seen is not None:
                del state[repo]
            continue

        if first_seen is None:
            state[repo] = now
            continue  # just went dirty — not yet worth a trigger

        age = now - first_seen
        if age >= threshold and len(triggers) < _MAX_TRIGGERS:
            hours = age / 3600
            triggers.append({
                "type": "dirty_worktree",
                "detail": f"{repo} has had uncommitted changes for {hours:.1f}h",
                "novelty": 0.5,
                "relevance": 0.7,
                "urgency": 0.4,
                "repo": repo,
                "dirty_hours": round(hours, 1),
            })
            # Reset the clock after reporting so this doesn't re-fire every
            # single cycle for the same still-dirty repo — re-arms after
            # another full threshold period if still dirty then.
            state[repo] = now

    _save_state(state)
    return triggers


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for t in scan_dirty_worktree():
        print(t["detail"])
