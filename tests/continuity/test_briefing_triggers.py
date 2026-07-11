"""Briefing trigger enumeration tests (issue #9, from EXP-001).

EXP-001 traced 1 of condition A's 3 missed canaries to briefing truncation:
the winning module batched several triggers into one bid, and the briefing's
"Why you're being woken" section carried only the bid's 200-char content
line — the canary won the workspace but its id never reached the model.
The fix: every trigger in the winning bid is enumerated in the briefing.
"""
from __future__ import annotations

import continuity.journal as journal


def _patch_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")


def _winner_with_triggers(details):
    return {
        "source_module": "infrastructure",
        "content": "CRITICAL: 3 infrastructure issue(s) — " + details[0],
        "salience": 0.86,
        "context": {
            "triggers": [
                {"type": "local_service_down", "detail": d, "urgency": 0.9}
                for d in details
            ]
        },
    }


def test_briefing_carries_every_trigger_in_winning_bid(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    details = [
        "[ID-AAA] first failure, this one is in the content line",
        "[ID-BBB] second failure, batched behind the first",
        "[ID-CCC] third failure, also batched",
    ]
    briefing = journal.build_briefing(None, winner=_winner_with_triggers(details))
    for d in details:
        assert d[:100] in briefing, f"briefing dropped batched trigger: {d}"


def test_single_trigger_already_in_content_adds_no_duplicate_list(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    winner = {
        "source_module": "daemon",
        "content": "Daemon: 1 task(s) failed — [ID-ZZZ] the only failure",
        "salience": 0.65,
        "context": {"triggers": [{"detail": "[ID-ZZZ] the only failure", "urgency": 0.9}]},
    }
    briefing = journal.build_briefing(None, winner=winner)
    assert briefing.count("[ID-ZZZ]") == 1


def test_other_criticals_ride_along_in_the_briefing(tmp_path, monkeypatch):
    """Critical digest: alarms that lost the cycle's selection must still
    reach the model on this wake (EXP-001: overlapping alarm windows vs
    one wake slot per tick)."""
    _patch_journal(tmp_path, monkeypatch)
    winner = _winner_with_triggers(["[ID-WIN] the winning failure"])
    losers = [{
        "source_module": "daemon",
        "context": {"triggers": [
            {"detail": "[ID-LOSER-1] queued alarm one", "urgency": 0.95},
            {"detail": "[ID-LOSER-2] queued alarm two", "urgency": 0.95},
        ]},
    }]
    briefing = journal.build_briefing(None, winner=winner, other_criticals=losers)
    assert "[ID-WIN]" in briefing
    assert "[ID-LOSER-1]" in briefing and "[ID-LOSER-2]" in briefing
    assert "Other active alarms" in briefing


def test_no_other_criticals_no_extra_section(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    briefing = journal.build_briefing(
        None, winner=_winner_with_triggers(["[ID-X] solo"]), other_criticals=[]
    )
    assert "Other active alarms" not in briefing


def test_trigger_list_is_capped_not_unbounded(tmp_path, monkeypatch):
    _patch_journal(tmp_path, monkeypatch)
    details = [f"[ID-{i:03d}] failure number {i}" for i in range(40)]
    briefing = journal.build_briefing(None, winner=_winner_with_triggers(details))
    assert "[ID-001]" in briefing
    assert "[ID-019]" in briefing
    assert "[ID-039]" not in briefing  # beyond the 20-trigger cap
    assert "+20 more" in briefing
