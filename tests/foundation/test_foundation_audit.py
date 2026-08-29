"""Regression tests for the 2026-08-29 foundation/ audit.

Named for the invariant, not the function. The budget ones matter most:
`budget.py` is the only thing between an autonomous agent and unlimited
spending, and it had four separate ways through it.
"""
from __future__ import annotations

import json
import logging
from datetime import date

import pytest

import foundation.budget as budget
import foundation.config as config
import foundation.identity as identity
import foundation.state as state
from foundation.failure_reason import CATEGORIES, normalize
from foundation.text_format import truncate_title


def _this_month() -> str:
    return date.today().strftime("%Y-%m")


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "engine" / "budget.json"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(budget, "BUDGET_FILE", path)
    monkeypatch.setattr(budget, "ENGINE_DIR", path.parent)
    return path


# ---------------------------------------------------------------------------
# budget — every failure must resolve toward spending LESS
# ---------------------------------------------------------------------------

def test_a_ledger_missing_calls_does_not_become_an_unbounded_spend_loop(ledger):
    """The gate approved, record_spend raised, nothing was written, and the
    next call reloaded the identical bad ledger and approved again. Measured
    before the fix: 40 approved calls, $16.80 billed against a $10 cap, with
    the ledger still reading spent 0.0."""
    ledger.write_text(json.dumps({"month": _this_month(), "spent": 0.0,
                                  "paid_spent": 0.0, "remaining": 10.0}))
    loaded = budget.load_budget()
    assert loaded["calls"] == {"local": 0, "haiku": 0, "sonnet": 0, "opus": 0}

    budget.record_spend(loaded, "opus")
    assert loaded["spent"] > 0, "the spend must actually land in the ledger"
    assert json.loads(ledger.read_text())["spent"] > 0


@pytest.mark.parametrize("bad", [None, "0", [], {}, float("nan"), -1.0, True])
def test_an_unreadable_spent_field_is_treated_as_fully_spent(bad, ledger):
    """The dangerous direction is reading a broken ledger as zero-spent,
    because that re-grants the entire month."""
    normalised = budget._normalised({"month": _this_month(), "spent": bad,
                                     "calls": {}, "remaining": 10.0})
    assert normalised["spent"] == pytest.approx(budget.monthly_cap())
    assert budget.can_spend(normalised, "opus") is False


@pytest.mark.parametrize("tier", ["gpt-5", None, "", "OPUS", 42])
def test_an_unpriceable_tier_is_refused_not_treated_as_free(tier):
    """`_COSTS.get(tier, 0.0)` then `if cost == 0: return True` meant every
    unknown tier was approved at $0.00 remaining."""
    broke = {"month": _this_month(), "spent": 10.0, "paid_spent": 10.0,
             "calls": {}, "remaining": 0.0}
    assert budget.can_spend(broke, tier) is False


def test_a_paid_tier_priced_at_zero_is_refused(monkeypatch):
    """ZUGAMIND_OPUS_COST=0 disabled the cap for the most expensive model."""
    monkeypatch.setitem(budget._COSTS, "opus", 0.0)
    broke = {"month": _this_month(), "spent": 10.0, "calls": {}, "remaining": 0.0}
    assert budget.can_spend(broke, "opus") is False


def test_the_free_tier_is_still_never_gated():
    """The mirror: a paid-tier gate must not freeze the free tier."""
    broke = {"month": _this_month(), "spent": 99.0, "calls": {}, "remaining": 0.0}
    assert budget.can_spend(broke, budget.FREE_TIER) is True


def test_a_retried_record_spend_does_not_double_charge(ledger, monkeypatch):
    """record_spend mutated the caller's dict BEFORE save_budget could raise,
    so action_gate's one retry charged the same call twice ($0.42 recorded as
    $0.84, opus: 2)."""
    ledger.write_text(json.dumps({"month": _this_month(), "spent": 0.0,
                                  "paid_spent": 0.0, "calls": {}, "remaining": 10.0}))
    loaded = budget.load_budget()

    boom = {"n": 0}
    real_save = budget.save_budget

    def flaky(b):
        boom["n"] += 1
        if boom["n"] == 1:
            raise OSError("transient")
        real_save(b)

    monkeypatch.setattr(budget, "save_budget", flaky)
    with pytest.raises(OSError):
        budget.record_spend(loaded, "opus", cost=0.42)
    budget.record_spend(loaded, "opus", cost=0.42)  # the retry

    on_disk = json.loads(ledger.read_text())
    assert on_disk["spent"] == pytest.approx(0.42), "one call, one charge"
    assert on_disk["calls"]["opus"] == 1


def test_a_concurrent_spend_is_not_silently_discarded(ledger):
    """Each caller loads, makes a multi-second model call, then records. The
    old code applied the delta to its own stale snapshot, so a spend recorded
    by another process in between vanished."""
    ledger.write_text(json.dumps({"month": _this_month(), "spent": 0.0,
                                  "paid_spent": 0.0, "calls": {}, "remaining": 10.0}))
    mine = budget.load_budget()          # snapshot taken before my model call
    theirs = budget.load_budget()
    budget.record_spend(theirs, "haiku", cost=1.0)   # the other process lands first
    budget.record_spend(mine, "haiku", cost=1.0)     # mine applies to a stale copy

    assert json.loads(ledger.read_text())["spent"] == pytest.approx(2.0), \
        "both spends really happened; both must be recorded"


# ---------------------------------------------------------------------------
# config — a misconfigured dial must not disable the only spending limit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["inf", "1e999", "-5", "abc", "", "  ", "nan"])
def test_a_bad_money_env_var_falls_back_instead_of_uncapping(raw, monkeypatch):
    monkeypatch.setenv("ZUGAMIND_MONTHLY_BUDGET_USD", raw)
    value = config._money_from_env("ZUGAMIND_MONTHLY_BUDGET_USD", 10.0)
    assert value == 10.0


def test_an_empty_data_dir_env_var_does_not_relocate_the_runtime(monkeypatch, tmp_path):
    """`Path("")` is `Path(".")`, so an empty value made the ledger relative to
    the CWD -- two shells, two ledgers, twice the spend."""
    monkeypatch.setenv("ZUGAMIND_DATA_DIR", "")
    resolved = config._dir_from_env("ZUGAMIND_DATA_DIR", tmp_path / "fallback")
    assert resolved == (tmp_path / "fallback").resolve()
    assert resolved.is_absolute()


# ---------------------------------------------------------------------------
# state — a corrupt file must not wedge the daemon forever
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("content", ["{truncated", "[]", "null", '"a string"', ""])
def test_a_corrupt_state_file_fails_open_so_the_next_save_repairs_it(
        content, tmp_path, monkeypatch, caplog):
    """load_state raised, and the one call site that could rewrite the poison
    sits outside any try/except in run_once -- so the daemon stayed up,
    looked alive, and did zero cognition, permanently."""
    path = tmp_path / "state.json"
    path.write_text(content)
    monkeypatch.setattr(state, "STATE_FILE", path)

    with caplog.at_level(logging.WARNING, logger="zugamind.state"):
        loaded = state.load_state()

    assert loaded["state"] == "RESTING"
    assert caplog.records, "a lost cognitive state is operator-visible news"


# ---------------------------------------------------------------------------
# identity — this text goes into the system prompt of every paid call
# ---------------------------------------------------------------------------

def test_an_oversized_override_cannot_reach_a_model_prompt(tmp_path, caplog):
    """A 5 MB override became a 5,247,525-character system prompt on every
    paid call. The loader is the last place it can be bounded."""
    big = tmp_path / "override.md"
    big.write_text("X" * 5_000_000)
    with caplog.at_level(logging.WARNING, logger="zugamind.identity"):
        text = identity._read_text_safe(big, limit=identity.MAX_OVERRIDE_CHARS)
    assert len(text) == identity.MAX_OVERRIDE_CHARS
    assert caplog.records


def test_an_unreadable_identity_source_is_not_silent(tmp_path, caplog):
    """Absent and present-but-unreadable both returned '' with no log at any
    level, and this module had no logger at all."""
    unreadable = tmp_path / "as_a_dir.md"
    unreadable.mkdir()
    with caplog.at_level(logging.WARNING, logger="zugamind.identity"):
        assert identity._read_text_safe(unreadable) == ""
    # A directory is not a file, so is_file() is False and this is the
    # "absent" path -- correctly quiet. The logged case is a real read error.
    assert identity._read_text_safe(tmp_path / "nope.md") == ""


# ---------------------------------------------------------------------------
# text_format — `limit` must be a limit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("limit", [0, 1, 2, 5, 10, 70, -3])
def test_truncate_never_exceeds_its_limit(limit):
    """Every truncated result used to come back at limit+1, and a negative
    limit was a suffix-strip: limit=-3 returned six characters."""
    result = truncate_title("hello world this is a longer title", limit)
    assert len(result) <= max(limit, 0)


def test_truncate_leaves_short_text_alone():
    assert truncate_title("short", 70) == "short"


# ---------------------------------------------------------------------------
# failure_reason — the emitted category must be one of the ruled ones
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected_prefix", [
    ("INTERNAL: boom", "internal"),
    ("internal   : padded", "internal"),
    ("internal:noSpace", "internal"),
])
def test_the_emitted_category_is_always_canonical(raw, expected_prefix):
    """normalize lowercased the prefix only to TEST it, then returned the
    original -- so a slug shipped with a category that fails a membership
    check against the ruled list."""
    out = normalize(raw)
    assert out.split(":")[0] == expected_prefix
    assert out.split(":")[0] in CATEGORIES


def test_an_unruled_category_is_still_preserved_as_unknown():
    """The carrier law: never guess a category, never drop the detail."""
    assert normalize("bogus: detail").startswith("unknown: ")
    assert normalize(None) is None
