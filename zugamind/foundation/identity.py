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

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("zugamind.identity")

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


# Hard ceiling on the local override. The override is agent-authored runtime
# text that gets spliced into the system prompt of every paid call, and the
# loader is the LAST place it can be bounded regardless of who wrote it.
# cognition/proposer.py caps what the automated path appends (2000 chars), but
# self_mod.propose() -- the public API, also reachable from `zugamind self-mod`
# -- applies no cap at all, so a hand-written 5 MB file became a 5,247,525
# character system prompt on every call (measured 2026-08-29).
MAX_OVERRIDE_CHARS = int(os.environ.get("ZUGAMIND_MAX_OVERRIDE_CHARS", "4000"))


def _read_text_safe(path: Path, limit: "int | None" = None) -> str:
    """Read text, return '' on any error. Never raises.

    A file that is ABSENT and a file that is present-but-unreadable both
    returned '' with no log at any level, and this module had no logger at
    all. Those are not the same event: the first is normal, the second is a
    broken deployment running with no identity at all, silently.
    """
    try:
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("identity source %s exists but could not be read (%s) — "
                       "continuing without it", path, e)
        return ""
    if limit is not None and len(text) > limit:
        logger.warning("identity override %s is %d chars — truncating to %d "
                       "before it reaches a model prompt", path, len(text), limit)
        return text[:limit]
    return text


def _assemble(facet: Facet) -> str:
    """Concat core files + local override, double-newline separated.

    Empty / unreadable blocks are skipped. Returns "" if nothing loaded.
    """
    blocks = [_read_text_safe(p) for p in facet.core_paths]
    override = _read_text_safe(facet.vault_override_path,
                               limit=MAX_OVERRIDE_CHARS).strip()
    if override:
        # Delimited, so the model can tell shipped charter from text the agent
        # wrote about itself at runtime. Unmarked, it read as one continuous
        # document with the runtime half in the most-recent position.
        blocks.append("--- LOCAL OVERRIDE (agent-authored at runtime) ---\n"
                      + override)
    assembled = "\n\n".join(b.strip() for b in blocks if b.strip())
    if not assembled and facet.core_paths:
        logger.warning("identity for facet %r assembled to EMPTY — the agent "
                       "is running with no persona", getattr(facet, "name", "?"))
    return assembled


def get_system_prompt(facet: Facet) -> str:
    """Return the full identity text for a facet.

    Concatenates: each core_path (in order) + local override (if it exists),
    double-newline separated, leading/trailing whitespace stripped per block.

    Returns "" if no sources are readable. Never raises.
    """
    return _assemble(facet)
