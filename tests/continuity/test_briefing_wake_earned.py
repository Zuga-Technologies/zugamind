"""The briefing must say whether the wake was EARNED (2026-08-17).

A bid's `salience` is rewritten in place by the attention schema before it
reaches the wake gate — x1.2 when another identity has held focus for 3
cycles, x1.1 when it isn't the current focus. Those multipliers exist to
share attention INSIDE the mind; they were never meant to authorise
spending a real session. Twice in one day a wake fired on a bid that had
asked BELOW the floor (17:54: 0.5164 -> 0.6816; 18:34: 0.60 -> 0.66,
against a 0.655 bar), and both times the woken session burned its context
re-deriving that from journal.jsonl because the briefing showed only the
post-boost number.

So: when raw and modulated differ, the briefing shows both, and when the
gate is judging the modulated number while the raw one sits below the bar,
it says so in as many words.
"""
from __future__ import annotations

import continuity.journal as journal


def _winner(raw, modulated):
    return {
        "source_module": "world_signals",
        "content": "[anthropic] some external signal",
        "salience": modulated,
        "context": {"raw_salience": raw, "top_type": "ai_lab_research"},
    }


def test_boosted_wake_below_the_bar_is_flagged_not_earned(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    monkeypatch.setattr(journal, "_wake_gate_hint", lambda: (0.655, "modulated"))

    briefing = journal.build_briefing(None, winner=_winner(0.60, 0.66))

    assert "NOT EARNED" in briefing
    assert "Bid 0.60" in briefing
    assert "0.655" in briefing


def test_wake_earned_on_its_own_bid_is_not_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    monkeypatch.setattr(journal, "_wake_gate_hint", lambda: (0.655, "modulated"))

    # Asked for 0.70 — above the bar on its own merit; the boost is incidental.
    briefing = journal.build_briefing(None, winner=_winner(0.70, 0.77))

    assert "NOT EARNED" not in briefing
    assert "Bid 0.70" in briefing


def test_raw_basis_gate_is_never_labelled_not_earned(tmp_path, monkeypatch):
    """Once the gate judges the raw number, a boost cannot carry anything —
    there is no unearned case left to flag, only the basis to report."""
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    monkeypatch.setattr(journal, "_wake_gate_hint", lambda: (0.58, "raw"))

    briefing = journal.build_briefing(None, winner=_winner(0.60, 0.66))

    assert "NOT EARNED" not in briefing
    assert "judged on raw" in briefing


def test_unmodulated_bid_adds_no_noise(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    monkeypatch.setattr(journal, "_wake_gate_hint", lambda: (0.655, "modulated"))

    briefing = journal.build_briefing(None, winner=_winner(0.66, 0.66))

    assert "Bid " not in briefing


def test_missing_gate_still_reports_both_numbers(tmp_path, monkeypatch):
    """The floor is a nice-to-have; raw-vs-modulated is the load-bearing part."""
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    monkeypatch.setattr(journal, "_wake_gate_hint", lambda: None)

    briefing = journal.build_briefing(None, winner=_winner(0.60, 0.66))

    assert "Bid 0.60, woke on 0.66" in briefing
    assert "NOT EARNED" not in briefing


def test_gate_hint_never_raises_into_the_briefing(monkeypatch):
    """A broken act/ layer must degrade to "no hint", not to no briefing."""
    import builtins
    real_import = builtins.__import__

    def boom(name, *a, **kw):
        if name == "act" or name.startswith("act."):
            raise ImportError("act layer unavailable")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", boom)
    assert journal._wake_gate_hint() is None
