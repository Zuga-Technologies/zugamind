"""Tests for scanners/scheduler.py — SourceScheduler.

Covers: disabled-by-default behavior (flag off -> everything always due, the
cadence/yield machinery is a no-op), enabled behavior via monkeypatched env
ZUGAMIND_SOURCE_SCHEDULER_ENABLED, cadence gating (a source polled recently
isn't due; a backed-off source uses max_cadence), yield recording
(record_yield / note_polled round-trip, yield_rate, emit_budget), the
source-ledger persistence path (patched to tmp_path), and the fail-closed
poll-all fallback in due_sources().

SourceScheduler is constructed directly in every test (not via the
get_scheduler() singleton) so state never leaks between tests, and so the
dynamic-scanner discovery in get_scheduler() (which shells out to git) is
never invoked.
"""
from __future__ import annotations

import json

import scanners.scheduler as scheduler
from scanners.scheduler import SourceScheduler, SourceSpec


def _patch_ledger(tmp_path, monkeypatch):
    ledger_path = tmp_path / "_source_ledger.json"
    monkeypatch.setattr(scheduler, "_LEDGER_PATH", ledger_path)
    return ledger_path


def _specs(**overrides):
    base = {
        "cheap": SourceSpec("cheap", base_cadence_secs=100),
        "always": SourceSpec("always", base_cadence_secs=100, always_on=True),
    }
    base.update(overrides)
    return base


# --- disabled-by-default: gating is a no-op ----------------------------------

def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("ZUGAMIND_SOURCE_SCHEDULER_ENABLED", raising=False)
    assert scheduler._flag_enabled() is False


def test_disabled_scheduler_everything_is_always_due(tmp_path, monkeypatch):
    _patch_ledger(tmp_path, monkeypatch)
    monkeypatch.delenv("ZUGAMIND_SOURCE_SCHEDULER_ENABLED", raising=False)

    sched = SourceScheduler(specs=_specs())
    now = 1_000_000.0
    # Poll "cheap" once, then immediately ask again — with the flag off this
    # must still be due (poll-all = today's behavior).
    sched.record_yield("cheap", 1, now=now)
    assert sched.due("cheap", now + 1) is True
    assert len(sched.due_sources(now + 1)) == len(sched.specs)


def test_disabled_scheduler_due_sources_returns_all_specs(tmp_path, monkeypatch):
    _patch_ledger(tmp_path, monkeypatch)
    monkeypatch.delenv("ZUGAMIND_SOURCE_SCHEDULER_ENABLED", raising=False)

    sched = SourceScheduler(specs=_specs())
    due = sched.due_sources(now=1_000_000.0)
    assert {s.name for s in due} == {"cheap", "always"}


# --- enabled behavior: cadence gating -----------------------------------------

def test_enabled_scheduler_never_polled_source_is_due(tmp_path, monkeypatch):
    _patch_ledger(tmp_path, monkeypatch)
    monkeypatch.setenv("ZUGAMIND_SOURCE_SCHEDULER_ENABLED", "1")

    sched = SourceScheduler(specs=_specs())
    assert sched.due("cheap", now=1_000_000.0) is True


def test_enabled_scheduler_recently_polled_source_is_not_due(tmp_path, monkeypatch):
    _patch_ledger(tmp_path, monkeypatch)
    monkeypatch.setenv("ZUGAMIND_SOURCE_SCHEDULER_ENABLED", "1")

    sched = SourceScheduler(specs=_specs())
    now = 1_000_000.0
    sched.record_yield("cheap", 0, now=now)  # stamps last_polled=now

    assert sched.due("cheap", now + 50) is False   # cadence is 100s
    assert sched.due("cheap", now + 100) is True    # exactly at cadence


def test_enabled_scheduler_always_on_bypasses_cadence(tmp_path, monkeypatch):
    _patch_ledger(tmp_path, monkeypatch)
    monkeypatch.setenv("ZUGAMIND_SOURCE_SCHEDULER_ENABLED", "1")

    sched = SourceScheduler(specs=_specs())
    now = 1_000_000.0
    sched.record_yield("always", 0, now=now)

    assert sched.due("always", now + 1) is True  # always_on ignores cadence


def test_various_truthy_flag_values(monkeypatch):
    for val in ("1", "true", "True", "yes", "on"):
        monkeypatch.setenv("ZUGAMIND_SOURCE_SCHEDULER_ENABLED", val)
        assert scheduler._flag_enabled() is True
    for val in ("0", "false", "", "nope"):
        monkeypatch.setenv("ZUGAMIND_SOURCE_SCHEDULER_ENABLED", val)
        assert scheduler._flag_enabled() is False


def test_unregistered_source_uses_default_spec(tmp_path, monkeypatch):
    _patch_ledger(tmp_path, monkeypatch)
    monkeypatch.setenv("ZUGAMIND_SOURCE_SCHEDULER_ENABLED", "1")

    sched = SourceScheduler(specs={})
    spec = sched.spec_for("never_registered")
    assert spec.name == "never_registered"
    assert spec.base_cadence_secs == scheduler._DEFAULT_BASE_CADENCE


def test_register_adds_and_overrides_specs(tmp_path, monkeypatch):
    _patch_ledger(tmp_path, monkeypatch)
    sched = SourceScheduler(specs={})
    sched.register(SourceSpec("new_source", base_cadence_secs=42))
    assert sched.spec_for("new_source").base_cadence_secs == 42

    sched.register(SourceSpec("new_source", base_cadence_secs=99))
    assert sched.spec_for("new_source").base_cadence_secs == 99


# --- effective_cadence backoff ------------------------------------------------

def test_effective_cadence_backs_off_after_full_dead_window(tmp_path, monkeypatch):
    _patch_ledger(tmp_path, monkeypatch)
    sched = SourceScheduler(specs=_specs())
    now = 1_000_000.0
    for i in range(scheduler._YIELD_WINDOW):
        sched.record_yield("cheap", 0, now=now + i)

    assert sched.effective_cadence("cheap") == sched.spec_for("cheap").max_cadence_secs


def test_effective_cadence_stays_base_with_any_novel_hit_in_window(tmp_path, monkeypatch):
    _patch_ledger(tmp_path, monkeypatch)
    sched = SourceScheduler(specs=_specs())
    now = 1_000_000.0
    for i in range(scheduler._YIELD_WINDOW):
        novel = 1 if i == 0 else 0
        sched.record_yield("cheap", novel, now=now + i)

    assert sched.effective_cadence("cheap") == sched.spec_for("cheap").base_cadence_secs


def test_effective_cadence_base_below_full_window(tmp_path, monkeypatch):
    _patch_ledger(tmp_path, monkeypatch)
    sched = SourceScheduler(specs=_specs())
    now = 1_000_000.0
    for i in range(scheduler._YIELD_WINDOW - 1):  # one short of the full window
        sched.record_yield("cheap", 0, now=now + i)

    assert sched.effective_cadence("cheap") == sched.spec_for("cheap").base_cadence_secs


# --- yield recording / note_polled round-trip ---------------------------------

def test_start_cycle_and_note_polled_round_trip(tmp_path, monkeypatch):
    _patch_ledger(tmp_path, monkeypatch)
    sched = SourceScheduler(specs=_specs())

    assert sched.polled_this_cycle() == set()
    sched.note_polled("cheap")
    sched.note_polled("always")
    assert sched.polled_this_cycle() == {"cheap", "always"}

    sched.start_cycle()
    assert sched.polled_this_cycle() == set()


def test_record_yield_stamps_last_polled_and_appends_window(tmp_path, monkeypatch):
    _patch_ledger(tmp_path, monkeypatch)
    sched = SourceScheduler(specs=_specs())
    sched.record_yield("cheap", 3, now=1_000_000.0)

    assert sched.ledger.last_polled["cheap"] == 1_000_000.0
    assert sched.ledger.yields["cheap"] == [3]


def test_record_yield_caps_window_at_yield_window(tmp_path, monkeypatch):
    _patch_ledger(tmp_path, monkeypatch)
    sched = SourceScheduler(specs=_specs())
    for i in range(scheduler._YIELD_WINDOW + 5):
        sched.record_yield("cheap", i, now=1_000_000.0 + i)

    window = sched.ledger.yields["cheap"]
    assert len(window) == scheduler._YIELD_WINDOW
    # oldest entries (0..4) should have been dropped; most-recent-last.
    assert window[-1] == scheduler._YIELD_WINDOW + 4
    assert 0 not in window


def test_yield_rate_no_data_is_optimistic(tmp_path, monkeypatch):
    _patch_ledger(tmp_path, monkeypatch)
    sched = SourceScheduler(specs=_specs())
    assert sched.yield_rate("never_polled") == 1.0


def test_yield_rate_computed_from_window(tmp_path, monkeypatch):
    _patch_ledger(tmp_path, monkeypatch)
    sched = SourceScheduler(specs=_specs())
    for novel in (1, 0, 0, 2):
        sched.record_yield("cheap", novel, now=1_000_000.0)
    assert sched.yield_rate("cheap") == 0.5  # 2 of 4 polls surfaced novel signal


# --- emit_budget ---------------------------------------------------------------

def test_emit_budget_squeezed_to_one_when_fully_dead(tmp_path, monkeypatch):
    _patch_ledger(tmp_path, monkeypatch)
    sched = SourceScheduler(specs=_specs())
    for i in range(scheduler._YIELD_WINDOW):
        sched.record_yield("cheap", 0, now=1_000_000.0 + i)
    assert sched.emit_budget("cheap", base_cap=3) == 1


def test_emit_budget_bonus_slot_for_high_yield(tmp_path, monkeypatch):
    _patch_ledger(tmp_path, monkeypatch)
    sched = SourceScheduler(specs=_specs())
    for _ in range(5):
        sched.record_yield("cheap", 1, now=1_000_000.0)
    assert sched.emit_budget("cheap", base_cap=3) == 4


def test_emit_budget_squeezed_for_full_low_yield(tmp_path, monkeypatch):
    _patch_ledger(tmp_path, monkeypatch)
    sched = SourceScheduler(specs=_specs())
    # Full window, low-but-nonzero rate (< 0.3): mostly zeros, a couple novel hits.
    novels = [0] * 18 + [1, 1]
    for i, n in enumerate(novels):
        sched.record_yield("cheap", n, now=1_000_000.0 + i)
    assert sched.emit_budget("cheap", base_cap=3) == 2


def test_emit_budget_defaults_to_base_cap_for_mid_yield(tmp_path, monkeypatch):
    _patch_ledger(tmp_path, monkeypatch)
    sched = SourceScheduler(specs=_specs())
    for novel in (1, 0):  # rate 0.5 — not full window, not >=0.6
        sched.record_yield("cheap", novel, now=1_000_000.0)
    assert sched.emit_budget("cheap", base_cap=3) == 3


def test_emit_budget_never_zero(tmp_path, monkeypatch):
    _patch_ledger(tmp_path, monkeypatch)
    sched = SourceScheduler(specs=_specs())
    for i in range(scheduler._YIELD_WINDOW):
        sched.record_yield("cheap", 0, now=1_000_000.0 + i)
    assert sched.emit_budget("cheap", base_cap=1) >= 1


# --- ledger persistence --------------------------------------------------------

def test_record_yield_persists_to_ledger_file(tmp_path, monkeypatch):
    ledger_path = _patch_ledger(tmp_path, monkeypatch)
    sched = SourceScheduler(specs=_specs())
    sched.record_yield("cheap", 2, now=1_000_000.0)

    assert ledger_path.exists()
    on_disk = json.loads(ledger_path.read_text("utf-8"))
    assert on_disk["last_polled"]["cheap"] == 1_000_000.0
    assert on_disk["yields"]["cheap"] == [2]


def test_new_scheduler_loads_persisted_ledger(tmp_path, monkeypatch):
    _patch_ledger(tmp_path, monkeypatch)
    sched1 = SourceScheduler(specs=_specs())
    sched1.record_yield("cheap", 5, now=1_000_000.0)

    sched2 = SourceScheduler(specs=_specs())  # fresh instance, same ledger path
    assert sched2.ledger.last_polled["cheap"] == 1_000_000.0
    assert sched2.ledger.yields["cheap"] == [5]


def test_corrupt_ledger_file_fails_open_to_fresh(tmp_path, monkeypatch):
    ledger_path = _patch_ledger(tmp_path, monkeypatch)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text("{not valid json", "utf-8")

    sched = SourceScheduler(specs=_specs())
    assert sched.ledger.last_polled == {}
    assert sched.ledger.yields == {}
    # And it must still be usable (no crash on subsequent record_yield).
    sched.record_yield("cheap", 1, now=1_000_000.0)
    assert sched.ledger.yields["cheap"] == [1]


def test_missing_ledger_parent_dir_is_created_on_save(tmp_path, monkeypatch):
    nested = tmp_path / "nested" / "dir" / "_source_ledger.json"
    monkeypatch.setattr(scheduler, "_LEDGER_PATH", nested)

    sched = SourceScheduler(specs=_specs())
    sched.record_yield("cheap", 1, now=1_000_000.0)
    assert nested.exists()


# --- fail-closed due_sources ----------------------------------------------------

def test_due_sources_fails_closed_to_poll_all_on_error(tmp_path, monkeypatch):
    _patch_ledger(tmp_path, monkeypatch)
    monkeypatch.setenv("ZUGAMIND_SOURCE_SCHEDULER_ENABLED", "1")

    sched = SourceScheduler(specs=_specs())

    def _boom(name, now):
        raise RuntimeError("scheduler bug")

    monkeypatch.setattr(sched, "due", _boom)
    due = sched.due_sources(now=1_000_000.0)
    assert {s.name for s in due} == {"cheap", "always"}
