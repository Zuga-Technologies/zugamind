"""ZugaMind workspace package — the operational Global Workspace Theory (GWT)
engine. Re-exports the public surface so callers can `from cognition.workspace
import X`.
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
from .workspace_actuator import WorkspaceActuator

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
    "WorkspaceActuator",
]
