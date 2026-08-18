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

So: when raw and modulated differ, the briefing shows both, plus the number
the gate ACTUALLY judged.

Updated 2026-08-18. The gate now clamps to min(bid, modulated) on either
fitted basis, so "a boost carried a bid below the bar" is unreachable —
that winner is filtered before a briefing is ever built (runner filters
harnesses at _harness_wants, then calls build_briefing). What replaced the
NOT-EARNED flag is the judged number itself, because the reverse case is
live: an alarm-lane winner bypasses the floor entirely, and a wake whose
bid was DAMPED must not be described as gate-approved on its raw bid.
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


def test_damped_wake_is_judged_on_the_damped_number(tmp_path, monkeypatch):
    """Asked 0.70, damped to 0.66: the clamp judges 0.66, and so must the note."""
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    monkeypatch.setattr(journal, "_wake_gate_hint", lambda: (0.655, "modulated"))

    briefing = journal.build_briefing(None, winner=_winner(0.70, 0.66))

    assert "Bid 0.70, woke on 0.66" in briefing
    assert "judged on min(bid, modulated) = 0.66" in briefing
    assert "0.655" in briefing


def test_alarm_lane_wake_never_claims_the_raw_bid_was_approved(tmp_path, monkeypatch):
    """The exact 2026-08-18 shape: raw 0.67, damped to 0.25, bar 0.600.

    The floor no longer passes this, but the alarm lane still bypasses the
    floor by design (EXP-003), so a heavily damped winner can reach a
    briefing. When it does, the note must show 0.25 — the number the gate
    would have judged — not the 0.67 the module asked for."""
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    monkeypatch.setattr(journal, "_wake_gate_hint", lambda: (0.600, "raw"))

    briefing = journal.build_briefing(None, winner=_winner(0.67, 0.25))

    assert "judged on min(bid, modulated) = 0.25" in briefing
    assert "judged on raw" not in briefing


def test_wake_earned_on_its_own_bid_is_not_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    monkeypatch.setattr(journal, "_wake_gate_hint", lambda: (0.655, "modulated"))

    # Asked for 0.70 — above the bar on its own merit; the boost is incidental.
    briefing = journal.build_briefing(None, winner=_winner(0.70, 0.77))

    assert "NOT EARNED" not in briefing
    assert "Bid 0.70" in briefing
    # The boost is discarded, not credited: the clamp judged the 0.70 bid.
    assert "judged on min(bid, modulated) = 0.70" in briefing


def test_note_names_the_fitted_series_and_the_judged_number(tmp_path, monkeypatch):
    """Which series the floor was FITTED on and which number it JUDGED are two
    different facts, and the woken session needs both to check its own wake."""
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    monkeypatch.setattr(journal, "_wake_gate_hint", lambda: (0.58, "raw"))

    briefing = journal.build_briefing(None, winner=_winner(0.60, 0.66))

    assert "NOT EARNED" not in briefing
    assert "fitted on the raw series" in briefing
    assert "judged on min(bid, modulated) = 0.60" in briefing


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
