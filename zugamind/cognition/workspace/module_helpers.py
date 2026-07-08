"""Shared scaffolding for WorkspaceModule bidders.

Small helpers factored out so extension modules don't each reinvent gate
flags, read-only DB connections, or self-registration into ALL_MODULES.

Stdlib-only. Each helper is best-effort and must never raise into a cycle.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable

from cognition.workspace.workspace import WorkspaceContent

logger = logging.getLogger("zugamind.workspace.module_helpers")


def gate_enabled(env_var: str, default: str = "1") -> bool:
    """Default-ON env gate: enabled unless explicitly '0'.

    A default-OFF capability (must be explicitly '1') has different
    semantics and should keep its own gate — don't route it through here.
    """
    return os.environ.get(env_var, default) != "0"


def ro_conn(db_path: str | Path) -> sqlite3.Connection:
    """Read-only SQLite connection (uri mode, 2s timeout)."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)


def emit_event(kind: str, payload: dict, *, caller: str) -> None:
    """Log a structured workspace event. Best-effort — never raises.

    The origin project published these to a shared event-stream table; this
    OSS version just logs them structured, so a deployer can wire their own
    sink (a logging handler, a metrics exporter, a database) without this
    module needing to know about it.
    """
    try:
        logger.info("[%s] %s :: %s", caller, kind, payload)
    except Exception as exc:
        logger.debug("%s: emit_event failed: %s", caller, exc)


def make_on_broadcast(module_name: str, run_fn: Callable[..., Any]) -> Callable:
    """Build an on_broadcast handler that runs `run_fn(bid_context=...)` only
    when THIS module won the workspace. Assign the result to the module class."""
    def _on_broadcast(self, content: WorkspaceContent) -> None:  # noqa: ARG001
        try:
            if not (content and content.bid and content.bid.source_module == module_name):
                return
            run_fn(bid_context=dict(content.bid.context or {}))
        except Exception as exc:
            logger.debug("%s.on_broadcast: %s", module_name, exc)
    return _on_broadcast


def self_register(module_cls: type, *, anchor_name: str = "PriorityGoalsModule") -> None:
    """Idempotently insert module_cls into workspace_modules.ALL_MODULES, just
    after the named anchor class if present, else appended."""
    try:
        from cognition.workspace import workspace_modules as _wm
        if module_cls in _wm.ALL_MODULES:
            return
        anchor = getattr(_wm, anchor_name, None)
        if anchor is not None and anchor in _wm.ALL_MODULES:
            _wm.ALL_MODULES.insert(_wm.ALL_MODULES.index(anchor) + 1, module_cls)
        else:
            _wm.ALL_MODULES.append(module_cls)
    except Exception as exc:
        logger.debug("%s self-register skipped: %s", module_cls.__name__, exc)
