"""Tests for foundation/failure_reason.py — ZugaMind's fleet failure_reason
carrier (tier-1 sitting #7).

Covers: the ruled local-slug -> fleet-category mapping table, the three
deliberate-skip/success exclusions (None in, None out), normalize()'s
truncation + unknown-wrap laws, and the invoke_error substring branch.
"""
from __future__ import annotations

import foundation.failure_reason as failure_reason


# --- CATEGORIES / MAX_LEN ----------------------------------------------------

def test_categories_are_the_ruled_13():
    assert len(failure_reason.CATEGORIES) == 13
    assert failure_reason.CATEGORIES == (
        "capacity", "incomplete", "infrastructure", "quality", "resource",
        "escalation", "unknown", "dependency", "auth", "rate_limit", "budget",
        "input", "internal",
    )


def test_max_len_is_200():
    assert failure_reason.MAX_LEN == 200


# --- normalize() --------------------------------------------------------------

def test_normalize_none_stays_none():
    assert failure_reason.normalize(None) is None


def test_normalize_blank_string_becomes_none():
    assert failure_reason.normalize("   ") is None


def test_normalize_ruled_category_prefix_passes_through():
    assert failure_reason.normalize("dependency: api_error:boom") == "dependency: api_error:boom"


def test_normalize_unrecognized_prefix_wraps_as_unknown():
    assert failure_reason.normalize("not_a_category: whatever") == "unknown: not_a_category: whatever"


def test_normalize_no_colon_wraps_as_unknown():
    assert failure_reason.normalize("just some text") == "unknown: just some text"


def test_normalize_truncates_to_max_len():
    long_detail = "x" * 400
    result = failure_reason.normalize(f"internal: {long_detail}")
    assert len(result) == failure_reason.MAX_LEN
    assert result.startswith("internal: ")


# --- map_local_slug(): None / excluded (deliberate-skip, success-shaped) ----

def test_map_local_slug_none_input_is_none():
    assert failure_reason.map_local_slug(None) is None


def test_map_local_slug_blank_string_is_none():
    assert failure_reason.map_local_slug("  ") is None


def test_map_local_slug_harness_disabled_excluded():
    assert failure_reason.map_local_slug("harness_disabled") is None


def test_map_local_slug_dry_run_excluded():
    assert failure_reason.map_local_slug("dry_run") is None


def test_map_local_slug_wake_filtered_excluded():
    assert failure_reason.map_local_slug("wake_filtered") is None


def test_map_local_slug_quiet_hours_deferred_excluded():
    assert failure_reason.map_local_slug("quiet_hours_deferred") is None


def test_map_local_slug_budget_not_persisted_excluded():
    """Degraded-success (ok:True law) — never a failure_reason, even though
    it looks failure-shaped as a string."""
    assert failure_reason.map_local_slug("budget_not_persisted:disk full") is None
    assert failure_reason.map_local_slug("budget_not_persisted") is None


# --- map_local_slug(): the ruled table ---------------------------------------

def test_rate_limited_maps_to_rate_limit_category():
    assert failure_reason.map_local_slug("rate_limited") == "rate_limit: rate_limited"


def test_rate_limited_enriched_detail_matches_bugas_worked_example():
    result = failure_reason.map_local_slug("rate_limited (hour cap: 3/3)")
    assert result == "rate_limit: rate_limited (hour cap: 3/3)"


def test_rate_limit_indeterminate_maps_to_infrastructure():
    assert failure_reason.map_local_slug("rate_limit_indeterminate") == \
        "infrastructure: rate_limit_indeterminate"


def test_budget_exhausted_maps_to_budget():
    assert failure_reason.map_local_slug("budget_exhausted") == "budget: budget_exhausted"


def test_bare_api_error_maps_to_dependency():
    assert failure_reason.map_local_slug("api_error") == "dependency: api_error"


def test_api_error_with_exc_maps_to_dependency():
    result = failure_reason.map_local_slug("api_error:Connection refused")
    assert result == "dependency: api_error:Connection refused"


def test_runner_error_maps_to_internal():
    result = failure_reason.map_local_slug("runner_error:KeyError('x')")
    assert result == "internal: runner_error:KeyError('x')"


def test_empty_command_maps_to_input():
    assert failure_reason.map_local_slug("empty_command") == "input: empty_command"


def test_timeout_maps_to_resource():
    assert failure_reason.map_local_slug("timeout") == "resource: timeout"


def test_import_error_maps_to_internal():
    result = failure_reason.map_local_slug("import_error:ModuleNotFoundError")
    assert result == "internal: import_error:ModuleNotFoundError"


def test_budget_error_maps_to_infrastructure():
    result = failure_reason.map_local_slug("budget_error:OSError('disk full')")
    assert result == "infrastructure: budget_error:OSError('disk full')"


def test_setup_error_maps_to_internal():
    result = failure_reason.map_local_slug("setup_error:PermissionError")
    assert result == "internal: setup_error:PermissionError"


def test_can_spend_error_maps_to_internal_not_infrastructure():
    """Buga's ruling: picked internal over infrastructure for consistency
    with setup_error — explicitly not a re-litigation."""
    result = failure_reason.map_local_slug("can_spend_error:RuntimeError")
    assert result == "internal: can_spend_error:RuntimeError"


def test_requires_human_review_maps_to_escalation():
    assert failure_reason.map_local_slug("requires_human_review") == \
        "escalation: requires_human_review"


def test_gate_not_ok_fallback_maps_to_unknown():
    assert failure_reason.map_local_slug("gate_not_ok") == "unknown: gate_not_ok"


# --- map_local_slug(): shield_refused (no forced category) ------------------

def test_shield_refused_wraps_as_unknown_with_full_detail():
    result = failure_reason.map_local_slug("shield_refused:destructive-shell")
    assert result == "unknown: shield_refused:destructive-shell"


def test_bare_shield_refused_wraps_as_unknown():
    assert failure_reason.map_local_slug("shield_refused") == "unknown: shield_refused"


# --- map_local_slug(): invoke_error substring branch -------------------------

def test_invoke_error_filenotfound_maps_to_infrastructure():
    result = failure_reason.map_local_slug(
        "invoke_error:[WinError 2] FileNotFoundError: cannot find file"
    )
    assert result.startswith("infrastructure: invoke_error:")


def test_invoke_error_permissionerror_maps_to_infrastructure():
    result = failure_reason.map_local_slug("invoke_error:PermissionError: [Errno 13]")
    assert result.startswith("infrastructure: invoke_error:")


def test_invoke_error_other_exception_maps_to_internal():
    result = failure_reason.map_local_slug("invoke_error:ValueError('bad argv')")
    assert result.startswith("internal: invoke_error:")


def test_bare_invoke_error_maps_to_internal():
    assert failure_reason.map_local_slug("invoke_error") == "internal: invoke_error"


def test_invoke_error_real_winerror2_text_falls_through_to_internal():
    """Honesty check, not a design flaw to silently paper over: Python's
    OSError.__str__ (FileNotFoundError's base) does NOT include the class
    name — confirmed against a real live journal.jsonl row:
    'invoke_error:[WinError 2] The system cannot find the file specified'.
    The ruled substring check is literal text-matching as specified ("simple
    substring check"), so a real missing-binary error on this machine
    currently resolves to internal:, not infrastructure:, because the
    triggering substring genuinely isn't in the text. Flagged in the build
    report as a finding, not silently masked here."""
    real_text = "invoke_error:[WinError 2] The system cannot find the file specified"
    result = failure_reason.map_local_slug(real_text)
    assert result == f"internal: {real_text}"


# --- map_local_slug(): unrecognized local slug never silently dropped -------

def test_unrecognized_local_slug_wraps_as_unknown_not_dropped():
    result = failure_reason.map_local_slug("some_future_error:detail")
    assert result == "unknown: some_future_error:detail"


# --- map_local_slug(): truncation --------------------------------------------

def test_map_local_slug_truncates_long_detail():
    long_exc = "x" * 400
    result = failure_reason.map_local_slug(f"api_error:{long_exc}")
    assert len(result) == failure_reason.MAX_LEN
    assert result.startswith("dependency: api_error:")
