"""ZugaMind identity loader — Facet abstraction.

Layers core identity (shipped in this package, `foundation/persona/`) with an
optional local override file an integrator maintains at runtime. Read-only by
design — never writes any file.

Stdlib only, matching the rest of ZugaMind.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from foundation.config import DATA_DIR

__all__ = ["Facet", "SENTINEL", "DELIBERATIVE", "get_system_prompt"]


@dataclass(frozen=True)
class Facet:
    """A self-aware role of the agent's identity.

    core_paths: shipped identity files (immutable, part of this package)
    vault_override_path: an optional local file an integrator curates at
        runtime (may not exist — that's fine, it's simply skipped)
    role_summary: one-line self-description for diagnostics
    """
    name: str
    core_paths: tuple[Path, ...]
    vault_override_path: Path
    role_summary: str


PERSONA_DIR = Path(__file__).resolve().parent / "persona"
OVERRIDES_DIR = DATA_DIR / "overrides"


SENTINEL = Facet(
    name="sentinel",
    core_paths=(
        PERSONA_DIR / "identity_anchors.md",
    ),
    vault_override_path=OVERRIDES_DIR / "sentinel.md",
    role_summary="the agent's always-on reflex — local model, fast, watchful",
)


DELIBERATIVE = Facet(
    name="deliberative",
    core_paths=(
        PERSONA_DIR / "identity_anchors.md",
        PERSONA_DIR / "bootstrap.md",
        PERSONA_DIR / "charter.md",
    ),
    vault_override_path=OVERRIDES_DIR / "deliberative.md",
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
