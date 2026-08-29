"""ZugaMind identity loader — Facet abstraction.

Layers core identity (shipped in this package, `foundation/persona/`) with an
optional local override file kept at runtime under DATA_DIR/overrides/.
Read-only by design — never writes any file. cognition/self_mod.py is the
ONE writer, and it borrows override_path() from here so there is exactly
one rule for where a facet's override lives.

The override path is resolved at CALL time, not import time (2026-08-29).
Tests redirect foundation.config.DATA_DIR per test; an import-time constant
here quietly kept the loader pointed at the live deployment's files while
the writer, resolving live, wrote somewhere else — the agent would have been
editing a file nothing reads.

Who reads this: gates/action_gate.py heads every local-tier system prompt
with get_system_prompt(SENTINEL) and every paid-tier one with
get_system_prompt(DELIBERATIVE), when ZUGAMIND_IDENTITY_PROMPT_ENABLED is
on. From v0.1.0 until 2026-08-29 nothing called it: the persona shipped and
no prompt ever carried it.

Stdlib only, matching the rest of ZugaMind.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["Facet", "SENTINEL", "DELIBERATIVE", "get_system_prompt",
           "override_path", "overrides_dir", "PERSONA_DIR"]

PERSONA_DIR = Path(__file__).resolve().parent / "persona"


def overrides_dir() -> Path:
    """DATA_DIR/overrides, read from foundation.config at CALL time."""
    from foundation.config import DATA_DIR  # noqa: WPS433 — call time, see module docstring
    return Path(DATA_DIR) / "overrides"


def override_path(facet_name: str) -> Path:
    """Where `facet_name`'s runtime override lives: DATA_DIR/overrides/<name>.md."""
    return overrides_dir() / f"{facet_name}.md"


@dataclass(frozen=True)
class Facet:
    """A self-aware role of the agent's identity.

    core_paths: shipped identity files (immutable, part of this package)
    role_summary: one-line self-description for diagnostics
    vault_override_path: the optional local override, resolved live (may
        not exist — that's fine, it's simply skipped)
    """
    name: str
    core_paths: tuple[Path, ...]
    role_summary: str

    @property
    def vault_override_path(self) -> Path:
        return override_path(self.name)


SENTINEL = Facet(
    name="sentinel",
    core_paths=(
        PERSONA_DIR / "identity_anchors.md",
    ),
    role_summary="the agent's always-on reflex — local model, fast, watchful",
)


DELIBERATIVE = Facet(
    name="deliberative",
    core_paths=(
        PERSONA_DIR / "identity_anchors.md",
        PERSONA_DIR / "bootstrap.md",
        PERSONA_DIR / "charter.md",
    ),
    role_summary="the agent's deliberative self — Claude-tier, considers and decides",
)


def _read_text_safe(path: Path) -> str:
    """Read text, return '' on any error. Never raises."""
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except (OSError, UnicodeDecodeError):
        return ""


def _assemble(facet: Facet) -> str:
    """Concat core files + local override, double-newline separated.

    Empty / unreadable blocks are skipped. Returns "" if nothing loaded.
    """
    blocks = [_read_text_safe(p) for p in facet.core_paths]
    blocks.append(_read_text_safe(facet.vault_override_path))
    return "\n\n".join(b.strip() for b in blocks if b.strip())


def get_system_prompt(facet: Facet) -> str:
    """Return the full identity text for a facet.

    Concatenates: each core_path (in order) + local override (if it exists),
    double-newline separated, leading/trailing whitespace stripped per block.

    Returns "" if no sources are readable. Never raises.
    """
    return _assemble(facet)
