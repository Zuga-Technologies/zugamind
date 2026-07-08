"""GitHub issues scanner: config gating, PR filtering, dedupe, trigger shape."""
import json

from scanners.world import github_issues


def _fake_issue(iid, number, title, pr=False):
    d = {
        "id": iid,
        "number": number,
        "title": title,
        "html_url": f"https://github.com/o/r/issues/{number}",
        "user": {"login": "someone"},
    }
    if pr:
        d["pull_request"] = {"url": "..."}
    return d


def test_off_when_no_repos_configured(monkeypatch):
    monkeypatch.delenv("ZUGAMIND_WATCH_REPOS", raising=False)
    assert github_issues.scan_github_issues() == []


def test_new_issues_become_triggers_and_dedupe(monkeypatch, tmp_path):
    monkeypatch.setenv("ZUGAMIND_WATCH_REPOS", "o/r")
    monkeypatch.setattr(github_issues, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(github_issues, "_CACHE_FILE", tmp_path / "github_issues.json")
    monkeypatch.setattr(
        github_issues, "_fetch_issues",
        lambda repo: [
            _fake_issue(11, 1, "Crash when the gate refuses"),
            _fake_issue(12, 2, "A pull request", pr=True),
        ],
    )

    triggers = github_issues.scan_github_issues()
    assert len(triggers) == 1  # PR filtered out
    t = triggers[0]
    assert t["type"] == "repo_issue"
    assert t["issue_number"] == 1
    assert "Crash when the gate refuses" in t["detail"]
    assert t["repo"] == "o/r"

    # Second sweep: same issue is seen, no re-trigger.
    assert github_issues.scan_github_issues() == []
    seen = json.loads((tmp_path / "github_issues.json").read_text())["seen"]
    assert 11 in seen


def test_fetch_failure_is_silent(monkeypatch, tmp_path):
    monkeypatch.setenv("ZUGAMIND_WATCH_REPOS", "o/r")
    monkeypatch.setattr(github_issues, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(github_issues, "_CACHE_FILE", tmp_path / "github_issues.json")

    def _boom(repo):
        raise OSError("network down")

    monkeypatch.setattr(github_issues, "_fetch_issues", _boom)
    assert github_issues.scan_github_issues() == []
