"""The WIRED/DARK/LIBRARY map in gates/__init__.py must be TRUE, not prose.

Three separate times on 2026-08-29 this repo turned out to contain code that
was complete, tested, documented and called by nothing:

  - five gate modules whose only "wiring" was a docstring saying so
  - work_claim.check_entity_grounding, written and tested, no call site
  - _sweep_stale_cursors, a real cleanup living in a hook nobody installed

Each read exactly like protection. None of it protected anything. The map at
the top of gates/__init__.py is the answer to "which gates actually run" --
and a map is only worth what its last edit was worth, so this test is what
makes it worth something. It fails when a gate labelled WIRED or DARK has no
caller outside gates/, and when one labelled LIBRARY has acquired one.

Both directions matter. The first is a lie that overstates protection. The
second is drift: someone wired a gate up and left the map saying it is
dormant, which is how the next reader concludes there is nothing to check.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import gates

_PACKAGE = Path(gates.__file__).resolve().parent
_REPO = _PACKAGE.parent.parent

# "  WIRED   action_gate    ..." — label + module, first line of each entry.
_ENTRY_RE = re.compile(r"^\s{2}(WIRED|DARK|LIBRARY)\s+([a-z_]+)\b", re.MULTILINE)

# What counts as "something outside gates/ uses this". A gate's own module and
# its tests do not count -- that is the whole point.
_SEARCH_ROOTS = ("zugamind", "examples", "demo.py")


def _map_entries() -> list:
    entries = _ENTRY_RE.findall(gates.__doc__ or "")
    assert entries, "gates/__init__.py has no WIRED/DARK/LIBRARY map to check"
    return entries


def _public_names(module_name: str) -> list:
    """Public callables the module offers, from __all__ or its def lines."""
    source = (_PACKAGE / f"{module_name}.py").read_text(encoding="utf-8")
    match = re.search(r"__all__\s*=\s*\[(.*?)\]", source, re.DOTALL)
    if match:
        names = re.findall(r"[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']", match.group(1))
    else:
        names = re.findall(r"^(?:def|class)\s+([A-Za-z][A-Za-z0-9_]*)",
                           source, re.MULTILINE)
    # A bare constant is not a call site worth searching for; keep callables.
    return [n for n in names if n and not n.isupper()]


def _callers_outside_gates(module_name: str) -> list:
    """Files outside gates/ that reference the module or one of its exports."""
    needles = [module_name] + _public_names(module_name)
    hits: set = set()
    for needle in needles:
        out = subprocess.run(
            ["git", "grep", "-l", "-w", needle, "--", *_SEARCH_ROOTS],
            cwd=_REPO, capture_output=True, text=True, timeout=30,
        )
        if out.returncode not in (0, 1):
            pytest.skip(f"git grep unavailable (rc={out.returncode})")
        for line in out.stdout.splitlines():
            path = line.strip().replace("\\", "/")
            if path and "/gates/" not in path:
                hits.add(path)
    return sorted(hits)


@pytest.mark.parametrize("label,module", _map_entries())
def test_the_map_matches_reality(label, module):
    callers = _callers_outside_gates(module)
    if label in ("WIRED", "DARK"):
        assert callers, (
            f"gates/__init__.py calls {module} {label}, but nothing outside "
            f"gates/ references it. Either wire it up or relabel it LIBRARY -- "
            f"a gate that reads as protection and protects nothing is the "
            f"exact failure this map exists to prevent."
        )
    else:
        assert not callers, (
            f"gates/__init__.py calls {module} LIBRARY (no caller), but these "
            f"reference it: {callers}. It got wired and the map was not "
            f"updated -- relabel it, or the next reader concludes there is "
            f"nothing to check here."
        )


def test_every_gate_module_appears_in_the_map():
    """A new gate must be classified, not quietly added."""
    on_disk = {p.stem for p in _PACKAGE.glob("*.py") if p.stem != "__init__"}
    in_map = {module for _, module in _map_entries()}
    missing = on_disk - in_map
    assert not missing, (
        f"gate module(s) {sorted(missing)} exist but are not in the "
        f"WIRED/DARK/LIBRARY map. Add them with an honest label."
    )
    phantom = in_map - on_disk
    assert not phantom, f"map lists {sorted(phantom)}, which no longer exist"
