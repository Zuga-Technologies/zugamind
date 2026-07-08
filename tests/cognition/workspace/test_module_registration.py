"""Sanity guard for the example module registry (workspace_modules.py)."""
import cognition.workspace.workspace_modules as wm
from cognition.workspace.workspace import WorkspaceModule


def test_all_modules_are_workspace_module_subclasses():
    for cls in wm.ALL_MODULES:
        assert issubclass(cls, WorkspaceModule)


def test_all_modules_have_unique_names():
    names = [cls.name for cls in wm.ALL_MODULES]
    assert len(names) == len(set(names)), f"duplicate module names: {names}"


def test_create_all_modules_instantiates_every_class():
    modules = wm.create_all_modules()
    assert len(modules) == len(wm.ALL_MODULES)
    assert {m.name for m in modules} == {cls.name for cls in wm.ALL_MODULES}


def test_trigger_type_to_module_covers_non_intrinsic_modules():
    for cls in wm.ALL_MODULES:
        for ttype in cls.TRIGGER_TYPES:
            assert wm.TRIGGER_TYPE_TO_MODULE[ttype] == cls.name


def test_route_triggers_to_modules_groups_by_type():
    modules = wm.create_all_modules()
    triggers = [
        {"type": "git_commit", "detail": "x"},
        {"type": "local_service_down", "service": "y"},
        {"type": "unknown_type_no_module"},
    ]
    wm.route_triggers_to_modules(triggers, modules)
    by_name = {m.name: m for m in modules}
    assert by_name["code_changes"]._triggers == [triggers[0]]
    assert by_name["infrastructure"]._triggers == [triggers[1]]
    # Unrecognized trigger types are simply not routed anywhere; no crash.
