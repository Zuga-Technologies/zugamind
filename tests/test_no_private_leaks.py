"""Static leak guard: fail CI if a banned private-fork string ever lands in
the public tree. Cheap, dumb, and exactly as strong as its list -- a real
example (stream.discord_filter imported from a private-fork file that was
never meant to depend on it) motivated adding this after a 2026-08 coupling
audit found no such check existed anywhere in this repo. Extend BANNED
whenever a future audit finds a new leak class."""
import pathlib
import subprocess

import pytest

_REPO_ROOT = pathlib.Path(__file__).parent.parent

BANNED = [
    "zugabot.ai",
    "/Users/zugabot/",                 # private-fork Mac Mini absolute path
    "E:/Programming/Zugabot",          # private-fork Windows path
    "E:\\Programming\\Zugabot",
    "zugabot_unified.db",
    "localhost:8000/api",              # private FastAPI backend
    "discord_filter",                  # the one module that must stay private
]

_TRACKED = subprocess.run(
    ["git", "-C", str(_REPO_ROOT), "ls-files", "*.py", "*.md", "*.toml", "*.cfg"],
    capture_output=True, text=True, check=True,
).stdout.splitlines()


@pytest.mark.parametrize("needle", BANNED)
def test_repo_has_no_banned_string(needle):
    hits = []
    for rel in _TRACKED:
        path = _REPO_ROOT / rel
        if path.name == "test_no_private_leaks.py":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if needle in text:
            hits.append(rel)
    assert not hits, f"banned string {needle!r} found in: {hits}"
