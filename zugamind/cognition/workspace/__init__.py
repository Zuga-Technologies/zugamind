"""ZugaMind workspace package — the operational Global Workspace Theory (GWT)
engine. Re-exports the public surface so callers can `from cognition.workspace
import X`.

Note: `workspace_actuator.WorkspaceActuator` is intentionally NOT re-exported
here. It's a complete, tested feedback loop (fairness over tens of cycles —
see its own module docstring) but nothing instantiates it: import it directly
from `cognition.workspace.workspace_actuator` if you want to wire it in
(issue #5).
"""
from .workspace import (
    Workspace,
    ThoughtType,
    SalienceBid,
    WorkspaceContent,
    WorkspaceModule,
    AttentionSchema,
)
from .workspace_modules import create_all_modules, route_triggers_to_modules
from .workspace_planner import WorkspacePlanner

__all__ = [
    "Workspace",
    "ThoughtType",
    "SalienceBid",
    "WorkspaceContent",
    "WorkspaceModule",
    "AttentionSchema",
    "create_all_modules",
    "route_triggers_to_modules",
    "WorkspacePlanner",
]
