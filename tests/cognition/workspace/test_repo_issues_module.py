"""RepoIssuesModule: routing, bid strength, verbatim titles in content."""
from cognition.workspace import create_all_modules, route_triggers_to_modules
from cognition.workspace.workspace_modules import RepoIssuesModule


def _trigger(number=7, title="Docs typo", urgent=False):
    if urgent:
        title = "Crash on startup"
    return {
        "type": "repo_issue",
        "detail": f"New issue #{number} on o/r: {title}",
        "issue_id": 1000 + number,
        "issue_number": number,
        "issue_title": title,
        "repo": "o/r",
    }


def test_repo_issue_routes_to_module():
    modules = create_all_modules()
    route_triggers_to_modules([_trigger()], modules)
    mod = next(m for m in modules if m.name == "repo_issues")
    assert len(mod._triggers) == 1


def test_bid_carries_title_verbatim_and_outbids_idle_modules():
    mod = RepoIssuesModule()
    mod.set_triggers([_trigger(number=3, title="Gate refuses valid intent")])
    bid = mod.generate_bid({})
    assert "Gate refuses valid intent" in bid.content
    assert "#3" in bid.content
    # Must comfortably outbid the idle-infrastructure floor (0.05) and the
    # priority-goals baseline (~0.5) so a new human issue wins a quiet cycle.
    # 0.7 floor chosen after rehearsal: 0.55 lost the salience^4 draw ~30%
    # of cycles against ambient modules.
    assert bid.salience >= 0.7


def test_urgent_titles_bid_higher():
    calm = RepoIssuesModule()
    calm.set_triggers([_trigger()])
    urgent = RepoIssuesModule()
    urgent.set_triggers([_trigger(urgent=True)])
    assert urgent.generate_bid({}).salience > calm.generate_bid({}).salience


def test_no_triggers_no_bid():
    assert RepoIssuesModule().generate_bid({}) is None
