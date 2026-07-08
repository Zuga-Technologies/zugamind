"""Tests for the value gate (gates/value_gate.py).

Covers the deterministic scorer fast-paths, rolling-rate math against a
temp DB, bid re-weighting (dampen low-value, boost high-value, min-sample
guard), and the dark-ship safety gate (flag off => no persistence, bids
untouched).
"""
import pytest

import gates.value_gate as vg
from cognition.workspace import ThoughtType, SalienceBid


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "value_scores.db")


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("ZUGAMIND_VALUE_GATE_ENABLED", "true")
    monkeypatch.setattr(vg, "_MIN_SAMPLES", 3)


def _bid(module, salience, ttype=None):
    ctx = {"triggers": [{"type": ttype}]} if ttype else {}
    return SalienceBid(source_module=module, content=f"{module} bid",
                       salience=salience, thought_type=ThoughtType.METACOGNITION,
                       context=ctx)


# --- layered judge ----------------------------------------------------------

def test_deliverable_defers_without_outcome(db):
    v, reason = vg._judge_value("code", "code_changes", "fixed the bug",
                                corr_id="c1", db_path=db)
    assert v is None and "deferred" in reason


def test_deliverable_reads_recorded_outcome(db, enabled):
    import sqlite3
    with sqlite3.connect(db) as conn:
        vg._ensure_table(conn)
        conn.execute("INSERT INTO value_scores (source_module, action, value, corr_id, status) "
                     "VALUES ('daemon','code',1,'cX','final')")
        conn.commit()
    assert vg._judge_value("code", "x", "", corr_id="cX", db_path=db)[0] == 1


def test_research_is_soft_credit_not_autozero(db):
    assert vg._judge_value("research", "arxiv", "wrote a note")[0] == 1
    assert vg._judge_value("research", "arxiv", "")[0] == 0


def test_cognition_scores_zero():
    assert vg._judge_value("none", "arxiv", "read a paper")[0] == 0
    assert vg._judge_value("reflect", "hackernews", "mused")[0] == 0


# --- persistence + rate -----------------------------------------------------

def test_rate_reflects_history(db, enabled):
    for _ in range(3):
        vg.score_action("arxiv", "none", "ai_lab_research", db_path=db)
    vg.score_action("arxiv", "research", "ai_lab_research", summary="note", db_path=db)
    rate, n = vg.value_rate("arxiv", "ai_lab_research", db_path=db)
    assert n == 4 and rate == pytest.approx(0.25)


def test_deferred_deliverable_written_pending(db, enabled):
    assert vg.score_action("daemon", "code", "daemon_result", corr_id="d1", db_path=db) is None
    assert vg.value_rate("daemon", "daemon_result", db_path=db) is None


# --- bid re-weighting -------------------------------------------------------

def test_low_value_type_dampened(db, enabled):
    for _ in range(4):
        vg.score_action("external_signal", "none", "hackernews_story", db_path=db)
    b = _bid("external_signal", 0.8, "hackernews_story")
    vg._apply_value_prior([b], db_path=db)
    assert b.salience < 0.8


def test_high_value_type_boosted(db, enabled):
    for _ in range(4):
        vg.score_action("daemon", "research", "daemon_result", summary="note", db_path=db)
    b = _bid("daemon", 0.5, "daemon_result")
    vg._apply_value_prior([b], db_path=db)
    assert b.salience > 0.5


def test_below_min_samples_not_reweighted(db, enabled):
    vg.score_action("news", "none", "ai_lab_research", db_path=db)  # only 1 sample (<3)
    b = _bid("news", 0.6, "ai_lab_research")
    vg._apply_value_prior([b], db_path=db)
    assert b.salience == 0.6


# --- dark-ship gate ---------------------------------------------------------

def test_flag_off_is_noop(db, monkeypatch):
    monkeypatch.delenv("ZUGAMIND_VALUE_GATE_ENABLED", raising=False)
    assert vg.score_action("arxiv", "code", "x", db_path=db) is None
    assert vg.value_rate("arxiv", "x", db_path=db) is None
    b = _bid("arxiv", 0.7, "x")
    bids, snap = vg._apply_value_prior([b], db_path=db)
    assert snap is None and b.salience == 0.7
