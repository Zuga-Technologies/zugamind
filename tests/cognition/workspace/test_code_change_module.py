"""CodeChangeModule salience branches.

Regression guard for the 2026-08-16 finding: pure Claude Code *session*
activity was reaching the high "this looks like a fix/bug" branch because
its transcript text happened to contain the substrings "fix" or "bug".
That branch is the only one that clears a wake floor, so the mistake cost
real wakes.
"""
import cognition.workspace.workspace_modules as wm


def _bid(triggers):
    module = wm.CodeChangeModule()
    module._triggers = list(triggers)
    return module.generate_bid({})


def _session(detail, project="Zugabot"):
    return {"type": "recent_code_change", "detail": detail, "project": project}


def test_no_triggers_means_no_bid():
    assert _bid([]) is None


def test_session_activity_quoting_a_fix_branch_stays_on_the_low_branch():
    # The wake at 2026-08-16T22:09Z: a branch name inside a chat message.
    bid = _bid([_session("says: Four agents building now: ``` fix/shadow-measures-candidate")])
    assert bid.salience <= 0.4
    assert bid.emotional_valence == 0.0
    assert "not necessarily a file edit" in bid.content


def test_bugabot_in_a_path_is_not_a_bug_report():
    bid = _bid([_session(r"Read E:\Programming\apps\gaming\BugaBot\music.js", "Whitehouse")])
    assert bid.salience <= 0.4


def test_real_code_change_mentioning_a_fix_still_bids_high():
    bid = _bid([{"type": "code_change", "detail": "fix: null deref in planner", "project": "zugamind"}])
    assert bid.salience > 0.4
    assert bid.emotional_valence == -0.2


def test_commits_without_issue_words_bid_on_the_commit_branch():
    bid = _bid([{"type": "git_commit", "detail": "feat: add scanner", "project": "zugamind"}])
    assert 0.2 < bid.salience <= 0.5
    assert bid.emotional_valence == 0.1


def test_issue_words_match_on_word_boundaries_only():
    assert wm._ISSUE_WORD_RE.search("bugfix: off-by-one")
    assert wm._ISSUE_WORD_RE.search("Fixed the planner")
    assert not wm._ISSUE_WORD_RE.search("BugaBot music.js")
    assert not wm._ISSUE_WORD_RE.search("prefix the path")
