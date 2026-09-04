"""Tests for examples/hooks/zugamind_context.py — the hook that reports what
ZugaMind found while you were away.

The formatter had no coverage at all until a wake session read its own
session-start block and found a blank line where a 900-second killed wake
child should have been. Both failure shapes below are verbatim from
zugamind/data/engine/journal.jsonl (2026-09-04 and 2026-09-03).
"""
from __future__ import annotations

import sys
from pathlib import Path

_HOOKS_DIR = Path(__file__).resolve().parent.parent.parent / "examples" / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import zugamind_context  # noqa: E402


_TIMEOUT = {
    "kind": "harness_invocation", "ts": "2026-09-04T18:54:35.523016+00:00",
    "harness": "claude-code", "ok": False, "error": "timeout",
    "timeout_sec": 900, "killed_tree": True, "failure_reason": "resource: timeout",
    "stdout": "", "stderr": "",
}

_API_ERROR = {
    "kind": "harness_invocation", "ts": "2026-09-03T13:28:13.328437+00:00",
    "harness": "claude-code", "ok": False, "returncode": 1,
    "stdout": "API Error: 500 Internal server error. This is a server-side "
              "issue, usually temporary — try again in a moment.\n",
    "stderr": "",
}

_SUCCESS = {
    "kind": "harness_invocation", "ts": "2026-09-04T13:06:25.764145+00:00",
    "harness": "claude-code", "ok": True,
    "stdout": "Both artifacts are landed: the verdict entry and STATUS.md.\n",
}


def test_timeout_is_not_reported_as_an_empty_result():
    """A killed wake child produced no stdout. Reporting that as a bare
    'wake result:' reads as 'it ran and had nothing to say' — the opposite
    of what happened."""
    line = zugamind_context._format_findings([_TIMEOUT])
    assert "wake FAILED" in line
    assert "timed out" in line and "900s" in line
    assert "wake result" not in line
    assert not line.rstrip().endswith("wake result (claude-code):")


def test_api_error_stdout_is_not_presented_as_the_wakes_finding():
    """stdout on a failed invocation is the failure text, not a result.
    Labelling it 'wake result' makes an API outage look like a conclusion."""
    line = zugamind_context._format_findings([_API_ERROR])
    assert "wake FAILED" in line
    assert "wake result" not in line


def test_successful_wake_still_reports_its_stdout_unchanged():
    line = zugamind_context._format_findings([_SUCCESS])
    assert "wake result (claude-code):" in line
    assert "Both artifacts are landed" in line
    assert "FAILED" not in line


def test_alarm_formatting_is_untouched():
    line = zugamind_context._format_findings(
        [{"kind": "alarm", "ts": "2026-09-04T00:00:00+00:00", "detail": "disk low"}]
    )
    assert line == "- [2026-09-04T00:00:00+00:00] alarm: disk low"
