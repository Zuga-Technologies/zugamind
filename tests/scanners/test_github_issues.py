"""GitHub issues scanner: config gating, PR/commented filtering, re-emit
semantics (an untriaged issue keeps triggering until the world changes)."""

from scanners.world import github_issues


def _fake_issue(iid, number, title, pr=False, comments=0):
    d = {
        "id": iid,
        "number": number,
        "title": title,
        "comments": comments,
        "html_url": f"https://github.com/o/r/issues/{number}",
        "user": {"login": "someone"},
    }
    if pr:
        d["pull_request"] = {"url": "..."}
    return d


def test_off_when_no_repos_configured(monkeypatch):
    monkeypatch.delenv("ZUGAMIND_WATCH_REPOS", raising=False)
    assert github_issues.scan_github_issues() == []


def test_untriaged_issues_trigger_and_keep_triggering(monkeypatch, tmp_path):
    monkeypatch.setenv("ZUGAMIND_WATCH_REPOS", "o/r")
    monkeypatch.setattr(github_issues, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(github_issues, "_CACHE_FILE", tmp_path / "github_issues.json")
    monkeypatch.setattr(
        github_issues, "_fetch_issues",
        lambda repo: [
            _fake_issue(11, 1, "Crash when the gate refuses"),
            _fake_issue(12, 2, "A pull request", pr=True),
            _fake_issue(13, 3, "Already answered", comments=2),
        ],
    )

    triggers = github_issues.scan_github_issues()
    assert len(triggers) == 1  # PR and commented issue filtered out
    t = triggers[0]
    assert t["type"] == "repo_issue"
    assert t["issue_number"] == 1
    assert "Crash when the gate refuses" in t["detail"]
    assert t["repo"] == "o/r"

    # Second sweep: still uncommented -> STILL triggers. Losing one workspace
    # draw must not silence a real issue; only a comment landing does.
    again = github_issues.scan_github_issues()
    assert len(again) == 1 and again[0]["issue_number"] == 1


def test_fetch_failure_is_silent(monkeypatch, tmp_path):
    monkeypatch.setenv("ZUGAMIND_WATCH_REPOS", "o/r")
    monkeypatch.setattr(github_issues, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(github_issues, "_CACHE_FILE", tmp_path / "github_issues.json")

    def _boom(repo):
        raise OSError("network down")

    monkeypatch.setattr(github_issues, "_fetch_issues", _boom)
    assert github_issues.scan_github_issues() == []
