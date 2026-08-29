"""No test may resolve a path into the LIVE deployment's data directory.

`zugamind/data/` on this machine is not a scratch dir — it is a running
dogfood deployment's state: its journal, its budget ledger, its seen-sets.
conftest's autouse fixture redirects everything to a tmp dir, and it does so
by hand: 25 explicit monkeypatch.setattr calls, one per module attribute.

Hand-maintained lists rot. Three separate modules were found writing into the
live tree on 2026-08-29 alone, all the same shape — a module does
`from foundation.config import DATA_DIR`, which is a BY-VALUE import, so it
holds its own binding that patching `foundation.config.DATA_DIR` never
reaches:

    scanners/world/github_issues.py  _CACHE_FILE bound at import
    scanners/scheduler.py            _LEDGER_PATH bound at import
    act/command_actuator.py          _briefing_dir() via an unpatched DATA_DIR
                                     -- this one left a real file in the live
                                     directory on every full-suite run

Eleven modules import a path constant by value today. This test is the thing
that notices when the twelfth arrives, or when someone adds an attribute to a
module conftest already patches. It does not care HOW a module stays isolated
(a patched attribute, or resolving per call like ai_labs and identity do) —
only that the result does not point at live state.
"""
from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest

import zugamind

# The real directory, deliberately NOT read through foundation.config —
# conftest has already redirected that, which is the whole point.
_LIVE_DATA = (Path(zugamind.__file__).resolve().parent / "data").resolve()

# Modules that reach the network or a live daemon on import are skipped: this
# test is about paths, and importing them here would be a side effect.
_SKIP_SUBSTRINGS = ("__main__", "cli",)


def _all_zugamind_modules() -> list:
    names = []
    for info in pkgutil.walk_packages(zugamind.__path__, prefix=""):
        if any(s in info.name for s in _SKIP_SUBSTRINGS):
            continue
        names.append(info.name)
    return sorted(names)


def _live_paths_in(module) -> list:
    """Attributes of `module` that are Paths inside the live data tree."""
    offenders = []
    for attr in dir(module):
        if attr.startswith("__"):
            continue
        try:
            value = getattr(module, attr)
        except Exception:
            continue
        if not isinstance(value, Path):
            continue
        try:
            resolved = value.resolve()
        except (OSError, ValueError):
            continue
        if resolved == _LIVE_DATA or _LIVE_DATA in resolved.parents:
            # Only FILE paths. A bare directory root (_DATA_DIR, ENGINE_DIR)
            # is an intermediate that modules derive from and never write to
            # directly; flagging those buries the real signal under eleven
            # harmless constants. The dangerous shape is a leaf bound at
            # import -- _CACHE_FILE, _LEDGER_PATH, JOURNAL_FILE -- because
            # THAT is what gets opened.
            if resolved.suffix:
                offenders.append(f"{attr} -> {resolved}")
    return offenders


@pytest.mark.parametrize("module_name", _all_zugamind_modules())
def test_no_module_attribute_points_at_live_data(module_name):
    """Every Path constant must resolve inside the isolated tmp dir."""
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # a module that cannot import is not this test's job
        pytest.skip(f"{module_name} not importable here: {type(exc).__name__}")

    offenders = _live_paths_in(module)
    assert not offenders, (
        f"{module_name} holds a path inside the LIVE deployment data dir "
        f"({_LIVE_DATA}) during a test:\n  " + "\n  ".join(offenders) +
        "\n\nThis is the by-value import trap: `from foundation.config import "
        "DATA_DIR` gives the module its own binding, so conftest patching "
        "foundation.config.DATA_DIR never reaches it. Fix by resolving the "
        "path per call (see foundation/identity.override_path or "
        "scanners/world/ai_labs._cache_file) — that fixes it for every future "
        "test — or, failing that, add the attribute to conftest's fixture."
    )


def test_the_live_data_dir_is_actually_somewhere_else_during_tests():
    """Guard the guard: if conftest ever stopped isolating DATA_DIR, every
    assertion above would pass vacuously by comparing the live tree to
    itself."""
    from foundation import config

    assert Path(config.DATA_DIR).resolve() != _LIVE_DATA, (
        "conftest is no longer redirecting DATA_DIR — the isolation fixture "
        "is not running, and every path test in this file is meaningless."
    )
