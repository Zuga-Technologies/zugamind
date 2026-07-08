"""Contract-shape guard for the workspace's cross-boundary surfaces.

In the private origin project, this test AST-compared workspace.py against a
second, independent GWT implementation living in a different (private)
repo — the two were required to stay aligned on SalienceBid field names,
WorkspaceContent.to_dict() keys, and the WorkspaceModule base method names,
even though they differed in every other respect (sync vs async, selection
power, module set).

This OSS release ships only the one implementation, so there is no second
layer to diff against. The test is kept (adapted) as a same-repo regression
guard: it pins the exact shape of the three "shared contract" surfaces so a
future refactor can't silently rename them without a red test — which is
exactly what would break a second implementation trying to interop with
this one, e.g. an async or cross-process rewrite.
"""
import ast
from pathlib import Path

WORKSPACE_PY = Path(__file__).resolve().parents[3] / "zugamind" / "cognition" / "workspace" / "workspace.py"

SHARED_BID_FIELDS = {
    "source_module", "content", "salience", "thought_type",
    "emotional_valence", "context",
}


def _tree() -> ast.Module:
    return ast.parse(WORKSPACE_PY.read_text(encoding="utf-8"))


def _dataclass_fields(tree: ast.Module, cls: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            return {
                n.target.id for n in node.body
                if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
            }
    return None


def _todict_keys(tree: ast.Module, cls: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for f in node.body:
                if isinstance(f, ast.FunctionDef) and f.name == "to_dict":
                    for sub in ast.walk(f):
                        if isinstance(sub, ast.Dict):
                            return {
                                k.value for k in sub.keys
                                if isinstance(k, ast.Constant) and isinstance(k.value, str)
                            }
    return None


def _has_methods(tree: ast.Module, cls: str, methods: set) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            names = {f.name for f in node.body if isinstance(f, ast.FunctionDef)}
            return methods <= names
    return False


def test_salience_bid_field_names_pinned():
    fields = _dataclass_fields(_tree(), "SalienceBid")
    assert fields is not None, "SalienceBid not found"
    assert fields == SHARED_BID_FIELDS, f"SalienceBid fields drifted: {fields ^ SHARED_BID_FIELDS}"


def test_workspace_content_todict_keys_pinned():
    keys = _todict_keys(_tree(), "WorkspaceContent")
    assert keys, "WorkspaceContent.to_dict not found"
    expected = {
        "source_module", "content", "salience", "thought_type",
        "emotional_valence", "context", "all_bids_count", "runner_up_module",
    }
    assert keys == expected, f"to_dict() serialization keys drifted: {keys ^ expected}"


def test_workspace_module_base_methods_present():
    assert _has_methods(_tree(), "WorkspaceModule", {"generate_bid", "on_broadcast"})
