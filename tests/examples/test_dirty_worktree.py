"""Tests for the dirty-worktree example scanner
(examples/custom-scanners/scan_dirty_worktree.py).

`examples/custom-scanners/` is not an importable dotted package (its dir name
has a dash and no __init__.py) so this test inserts it onto sys.path directly,
the same way test_agent_reach.py does.

These build REAL temp git repos rather than monkeypatching `git status` — the
whole behaviour under test is how porcelain output gets classified, and a faked
subprocess would only assert that the fake matches the parser.

Origin (2026-08-31): a wake fired on "Backrooms has had uncommitted changes for
20.1h" at full strength. Every one of the 29 dirty paths was a .blend scene or a
directory of render .png — output a tool wrote. Nothing tracked was modified;
no carried work existed to lose. `_is_generated` knew lockfiles and node_modules
but nothing about render output, so the whole set counted as hand-authored and
bought a real agent session.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SCANNER_DIR = Path(__file__).resolve().parent.parent.parent / "examples" / "custom-scanners"
if str(_SCANNER_DIR) not in sys.path:
    sys.path.insert(0, str(_SCANNER_DIR))

import scan_dirty_worktree as sdw  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _make_repo(tmp_path: Path) -> Path:
    """A real git repo with one commit, so HEAD exists and the tree is clean."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@test")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "seed")
    return repo


def _armed_scan(monkeypatch, tmp_path, repo: Path) -> list[dict]:
    """Run the scanner with the dirty clock already older than the threshold."""
    monkeypatch.setattr(sdw, "_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setenv("ZUGAMIND_WATCH_WORKTREES", str(repo))
    monkeypatch.setenv("ZUGAMIND_DIRTY_THRESHOLD_HOURS", "24")

    # First pass only arms the clock (by design) and never triggers.
    assert sdw.scan_dirty_worktree() == []
    # Rewind first_seen well past the threshold instead of sleeping.
    state = sdw._load_state()
    state[str(repo)]["first_seen"] -= 30 * 3600
    sdw._save_state(state)
    return sdw.scan_dirty_worktree()


# ------------------------------------------------------- _is_generated unit --

def test_is_generated_flags_render_output():
    """The exact artifact classes the Backrooms wake fired on."""
    for path in (
        "evidence/room3/render_v27.blend",
        "evidence/desk_v4/contact_sheet.png",
        "evidence/radiator_v6/hero.PNG",       # case-insensitive
        "evidence/clips/walkthrough.mp4",
        "SourceArt/props/desk_high.glb",
        "evidence" + chr(92) + "room3" + chr(92) + "render_v12.blend",  # windows seps
    ):
        assert sdw._is_generated(path), path


def test_is_generated_leaves_real_source_alone():
    """Over-suppression is the failure mode that would make this scanner useless."""
    for path in (
        "tools/blender/make_room_level0.py",
        "Source/Backrooms/BackroomsCharacter.cpp",
        "harness/DECISIONS.md",
        "assets/icon.svg",                     # text, hand-edited
        "docs/blender-notes.md",
    ):
        assert not sdw._is_generated(path), path


# ------------------------------------------------------------- end-to-end --

def test_render_output_only_does_not_buy_a_session(monkeypatch, tmp_path):
    """The Backrooms case: untracked binaries only -> weak, below the wake floor."""
    repo = _make_repo(tmp_path)
    (repo / "evidence").mkdir()
    (repo / "evidence" / "render_v27.blend").write_bytes(b"BLENDER" * 64)
    (repo / "evidence" / "contact_sheet.png").write_bytes(b"PNGDATA" * 64)

    triggers = _armed_scan(monkeypatch, tmp_path, repo)

    assert len(triggers) == 1
    t = triggers[0]
    assert t["dirty_files"] == 2
    assert t["authored_files"] == 0
    assert "(generated files only)" in t["detail"]
    # The bid WorldSignals computes: 0.25 + 0.4*rel + 0.2*urg.
    bid = 0.25 + 0.4 * t["relevance"] + 0.2 * t["urgency"]
    assert bid < 0.5, f"render output alone should not clear a wake floor (got {bid:.2f})"


def test_untracked_directory_is_expanded_not_judged_by_its_name(monkeypatch, tmp_path):
    """The actual root cause, guarded on its own.

    Without `-uall`, git reports a wholly-untracked directory as ONE record
    (`?? evidence/desk_v4/`). That path has no extension, so _is_generated
    cannot classify it and the whole tree counts as authored no matter what
    is inside. This mirrors the real Backrooms shape: a prop-render dir of
    contact sheet + hero + six views, all of it tool output.
    """
    repo = _make_repo(tmp_path)
    prop = repo / "evidence" / "desk_v4"
    prop.mkdir(parents=True)
    for name in ("contact_sheet.png", "hero.png", *(f"view_{i}.png" for i in range(6))):
        (prop / name).write_bytes(b"PNGDATA" * 32)

    triggers = _armed_scan(monkeypatch, tmp_path, repo)

    assert len(triggers) == 1
    t = triggers[0]
    # 8 real files, not 1 collapsed directory record.
    assert t["dirty_files"] == 8, "untracked dir was not expanded into its files"
    assert t["authored_files"] == 0
    assert "(generated files only)" in t["detail"]


def test_one_real_source_file_still_wakes(monkeypatch, tmp_path):
    """Regression guard: binaries must not mask genuine carried work beside them."""
    repo = _make_repo(tmp_path)
    (repo / "evidence").mkdir()
    (repo / "evidence" / "render_v27.blend").write_bytes(b"BLENDER" * 64)
    (repo / "half_finished.py").write_text("def unfinished():\n    ...\n", encoding="utf-8")

    triggers = _armed_scan(monkeypatch, tmp_path, repo)

    assert len(triggers) == 1
    t = triggers[0]
    assert t["authored_files"] == 1
    assert "(generated files only)" not in t["detail"]
    bid = 0.25 + 0.4 * t["relevance"] + 0.2 * t["urgency"]
    assert bid > 0.5, f"a real uncommitted source file must still wake (got {bid:.2f})"
