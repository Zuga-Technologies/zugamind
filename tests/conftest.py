"""Make the zugamind package importable two ways for the test suite:

  - package-form:  `from zugamind.scanners.world import hackernews`
  - bare-form:     `from cognition...`, `from gates...`, `from foundation...`

Bare-form imports mirror the internal convention used throughout the
zugamind package itself (every module does `from foundation.config import
X`, not `from zugamind.foundation.config import X`), so both the repo root
and the zugamind/ package dir go on sys.path.
"""
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ZUGAMIND_PKG_DIR = os.path.join(_REPO_ROOT, "zugamind")

for _p in (_REPO_ROOT, _ZUGAMIND_PKG_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture(autouse=True)
def _isolate_live_data_dir(tmp_path, monkeypatch):
    """Safety net, not decoration: redirect EVERY module-level path derived
    from ENGINE_DIR/DATA_DIR to a per-test tmp dir, for every test, by
    default.

    On this machine `zugamind/data/` is not a scratch directory — it's the
    LIVE bugapc-claude-observer dogfood deployment's real runtime state
    (journal.jsonl, state.json, budget.json, ...), read and written by an
    actual autonomous process, mid an EXP-005 "value of wakes" observation
    window that runs through 2026-07-20 and is scored against that real
    journal. A test that forgets to patch some ENGINE_DIR-derived constant
    doesn't fail loud — it silently writes synthetic toy-scanner data into
    the real deployment's files, corrupting the exact data EXP-005 is being
    graded on.

    This is the same failure shape as the quiet-hours leak that hit
    EXP-002/003 AND unit tests in one day (2026-07-12, see run_exp001.py) —
    a test/experiment defaulting to real machine state instead of an
    isolated one. That lesson was "isolate every config seam"; this fixture
    is that discipline applied structurally, once, instead of per-test —
    added after `workspace_modules.PriorityGoalsModule.STATE_FILE` (a NEW
    persisted path, fixing the stale hours_stale bug) turned out to be a
    class attribute baked in at import time, which no per-test monkeypatch
    was isolating because the attribute didn't exist when those tests were
    written.

    A test that wants to exercise the REAL default path explicitly still
    can — call the function with an explicit path argument, or monkeypatch
    right back to the real constant — this fixture only changes what a test
    gets by NOT patching anything.
    """
    import act.command_actuator as _command_actuator
    import act.floor_calibration as _floor_calibration
    import cognition.workspace.workspace_actuator as _workspace_actuator
    import cognition.workspace.workspace_modules as _workspace_modules
    import continuity.journal as _journal
    import foundation.budget as _budget
    import foundation.config as _config
    import foundation.state as _state
    import scanners.scheduler as _scheduler
    import scanners.world.ai_labs as _ai_labs
    import scanners.world.github_issues as _github_issues
    import scanners.world.github_repo_events as _github_repo_events
    import scanners.world.hackernews as _hackernews

    engine_dir = tmp_path / "engine"
    data_dir = tmp_path / "data"
    cache_dir = data_dir / "scanner_cache"

    # ZUGAMIND_DATA_DIR: foundation.config.DATA_DIR reads this at import
    # time (already too late to matter, hence the direct attribute patches
    # below) — but scanners/world/reddit_ai.py deliberately reads it fresh
    # on every call ("stays standalone, without importing foundation"), so
    # the env var is the ONLY way to isolate that one.
    monkeypatch.setenv("ZUGAMIND_DATA_DIR", str(data_dir))

    # foundation.config's own module attributes — every ENGINE_DIR/DATA_DIR-
    # derived constant it defines, not just the two directories, because a
    # lazy `from foundation import config as _config; ...; _config.X` call
    # site (e.g. scanners/__init__.py's habituation cache) reads these
    # directly off the module and won't see a patched ENGINE_DIR/DATA_DIR
    # retroactively — X was already computed once, at config.py's own
    # import time.
    monkeypatch.setattr(_config, "ENGINE_DIR", engine_dir)
    monkeypatch.setattr(_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(_config, "STATE_FILE", engine_dir / "state.json")
    monkeypatch.setattr(_config, "BUDGET_FILE", engine_dir / "budget.json")
    monkeypatch.setattr(_config, "EVENT_LOG", engine_dir / "events.jsonl")
    monkeypatch.setattr(_config, "TRIGGERS_FILE", engine_dir / "triggers.json")
    monkeypatch.setattr(_config, "SEEN_TRIGGERS_FILE", engine_dir / "seen_triggers.json")

    # Modules that import one of the above BY VALUE (`from foundation.config
    # import BUDGET_FILE`) hold their own separate name binding — patching
    # foundation.config's attribute above does not reach it. Each such
    # module needs its own copy patched too. (This is exactly the gap that
    # let test_record_spend_deducts_and_bumps_call_counter silently
    # overwrite the real live budget.json on every pytest run since this
    # repo's initial release, until this fixture existed.)
    monkeypatch.setattr(_state, "ENGINE_DIR", engine_dir)
    monkeypatch.setattr(_state, "STATE_FILE", engine_dir / "state.json")
    monkeypatch.setattr(_journal, "JOURNAL_FILE", engine_dir / "journal.jsonl")
    monkeypatch.setattr(_budget, "ENGINE_DIR", engine_dir)
    monkeypatch.setattr(_budget, "BUDGET_FILE", engine_dir / "budget.json")
    monkeypatch.setattr(_workspace_actuator, "ENGINE_DIR", engine_dir)
    monkeypatch.setattr(_workspace_actuator, "CPPS_FILE", engine_dir / "workspace_cpps.jsonl")
    monkeypatch.setattr(_workspace_actuator, "ACTUATOR_STATE_FILE", engine_dir / "actuator_state.json")
    monkeypatch.setattr(_workspace_modules.PriorityGoalsModule, "STATE_FILE",
                        engine_dir / "priority_goals_state.json")
    monkeypatch.setattr(_command_actuator, "DEFAULT_HARNESS_CONFIG", data_dir / "harness.json")
    monkeypatch.setattr(_floor_calibration, "STATE_FILE", data_dir / "floor_calibration.json")
    monkeypatch.setattr(_scheduler, "_LEDGER_PATH", cache_dir / "_source_ledger.json")
    monkeypatch.setattr(_ai_labs, "_CACHE_DIR", cache_dir)
    monkeypatch.setattr(_github_issues, "_CACHE_DIR", cache_dir)
    monkeypatch.setattr(_github_repo_events, "_CACHE_FILE", cache_dir / "github_repo_events.json")
    monkeypatch.setattr(_hackernews, "_CACHE_PATH", cache_dir / "hackernews.json")
