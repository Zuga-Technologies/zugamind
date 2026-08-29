"""Cross-check the local ledger against what Anthropic actually billed.

WHAT THIS IS, in one sentence: `budget.json` is a tally of *estimates* this
agent wrote about itself, and this module asks the provider what it really
charged -- so the one number that gates spending can be checked against an
authority that is not itself.

`budget_reconcile.py` is the sibling of this file and closes a DIFFERENT gap.
It repairs spends this agent knows it made and failed to write down
(`budget_persist_failed` in the journal). It needs no credential and no
network, which is why it is the default repair. This module cannot repair
anything -- it can only tell you the local number disagrees with the bill,
which is the failure `budget_reconcile` cannot see: a per-call cost estimate
that is simply wrong, drifting the ledger a little on every single call.

THREE THINGS THAT MAKE THIS EASY TO GET WRONG. All three are handled here and
each has a test:

1. `amount` is a decimal string in the LOWEST CURRENCY UNIT -- cents, not
   dollars. The API reference's own example is `"123.78912"` for $1.2378912.
   Read it as dollars and every figure is 100x too large. This file converts
   in exactly one place (`_amount_to_usd`).

2. The cost report is ORG-WIDE. It bills every key in the organization --
   Claude Code, any other app, a teammate's script -- not just this agent.
   So `provider_total > ledger_spent` is the NORMAL reading and is not
   evidence of anything. Set `ZUGAMIND_ANTHROPIC_WORKSPACE_ID` to this
   agent's workspace to get a comparable number; without it, the comparison
   is reported as `scope: "organization"` and deliberately draws no
   conclusion. A drift number computed from an unfiltered org total would be
   a confident, meaningless figure -- the exact thing this codebase keeps
   getting burned by.

3. It needs an ADMIN credential, which is not the same thing as an API key.
   `sk-ant-admin01-...`, an OAuth token with `org:admin`, or a personal /
   service-account key that is not workspace-scoped. A workspace-scoped
   `sk-ant-api...` key returns 401 here no matter how valid it is for
   /v1/messages, and the Admin API is unavailable to individual (non-
   organization) accounts entirely. `describe_credential()` reports what is
   present WITHOUT ever returning or logging the value.

Stdlib only (urllib). Never raises into a caller; every failure comes back as
`error` on the summary dict.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from typing import Any, Optional
from urllib.parse import urlencode

logger = logging.getLogger("zugamind.cost_report")

COST_REPORT_URL = "https://api.anthropic.com/v1/organizations/cost_report"
ANTHROPIC_VERSION = "2023-06-01"
USER_AGENT = "ZugaMind/1.0 (budget reconciliation)"

# The whole point of note 1 in the docstring. Do not inline this.
_CENTS_PER_DOLLAR = 100.0

# Names checked in order. The first that holds a value wins. ANTHROPIC_API_KEY
# is last and is a LONG SHOT on purpose: it is usually a workspace-scoped key,
# which the Admin API rejects -- but a personal/service-account key stored
# under that name does work, and refusing to try it would be guessing.
_CREDENTIAL_NAMES = (
    "ZUGAMIND_ANTHROPIC_ADMIN_KEY",
    "ANTHROPIC_ADMIN_KEY",
    "ANTHROPIC_API_KEY",
)

# Prefix -> (kind, will the Admin API accept it?). Prefix only: this module
# never handles more of a credential than its first few characters outside of
# the one line that sets the request header.
_PREFIXES = (
    ("sk-ant-admin", "admin", True),
    ("sk-ant-oat", "oauth", True),
    ("sk-ant-api", "standard", None),  # None = only the API can say
)

_MAX_PAGES = 12          # 31-day buckets cap the page count; this is slack
_MAX_BODY_BYTES = 4 << 20
_TIMEOUT = 30


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A credentialed request does not follow redirects. Ever.

    urllib copies every header -- including the credential -- onto the
    redirect target with no same-host check. There is no legitimate redirect
    on this endpoint, so any redirect is either a misconfiguration or someone
    collecting admin keys.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise urllib.error.HTTPError(
            req.full_url, code,
            f"refusing to follow a redirect on a credentialed request ({code})",
            headers, fp)


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect)


# ---------------------------------------------------------------------------
# credential -- reported, never returned
# ---------------------------------------------------------------------------

def _raw_credential() -> "tuple[Optional[str], Optional[str]]":
    """(env var name, value) for the first credential present. Internal."""
    for name in _CREDENTIAL_NAMES:
        raw = (os.environ.get(name) or "").strip()
        if raw:
            return name, raw
    return None, None


def describe_credential() -> "dict[str, Any]":
    """What credential is present, said out loud without exposing it.

    Returns the env var NAME, the kind inferred from its prefix, and its
    length -- enough to answer "do I have the right kind of key" and not
    enough to use it. Nothing here is logged.
    """
    name, raw = _raw_credential()
    if not raw:
        return {
            "present": False, "name": None, "kind": None, "length": 0,
            "usable": False,
            "detail": "no credential found under " + ", ".join(_CREDENTIAL_NAMES),
        }

    kind, usable = "unknown", None
    for prefix, kind_name, accepted in _PREFIXES:
        if raw.startswith(prefix):
            kind, usable = kind_name, accepted
            break

    detail = {
        "admin": "an Admin API key -- the Admin API will accept this",
        "oauth": "an OAuth token -- usable if it carries the org:admin scope",
        "standard": ("a standard API key -- the Admin API accepts it ONLY if it "
                     "is a personal or service-account key; a workspace-scoped "
                     "key returns 401 here. Run the check to find out."),
        "unknown": ("prefix not recognised -- may not be an Anthropic credential "
                    "at all"),
    }[kind]

    return {"present": True, "name": name, "kind": kind, "length": len(raw),
            "usable": usable, "detail": detail}


def _auth_headers(raw: str) -> "dict[str, str]":
    """The header pair for this credential. The ONLY place a value is used."""
    base = {"anthropic-version": ANTHROPIC_VERSION, "User-Agent": USER_AGENT}
    if raw.startswith("sk-ant-oat"):
        base["Authorization"] = f"Bearer {raw}"
    else:
        base["x-api-key"] = raw
    return base


# ---------------------------------------------------------------------------
# amounts
# ---------------------------------------------------------------------------

def _amount_to_usd(amount: Any) -> float:
    """`"123.78912"` cents -> 1.2378912 dollars. See note 1 in the docstring."""
    try:
        return float(amount) / _CENTS_PER_DOLLAR
    except (TypeError, ValueError):
        logger.warning("cost_report: unparseable amount %r -- counted as 0", amount)
        return 0.0


def month_window(when: "date | None" = None) -> "tuple[str, str]":
    """(starting_at, ending_at) covering the current month to date, RFC 3339.

    The ledger is month-keyed with no daily reset, so this is the window that
    makes the two numbers comparable at all.
    """
    today = when or datetime.now(timezone.utc).date()
    start = today.replace(day=1)
    return (f"{start.isoformat()}T00:00:00Z",
            f"{today.isoformat()}T23:59:59Z")


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

def _http_get_json(url: str, headers: dict) -> dict:
    """One GET. Split out so tests can replace it without a network."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    with _opener().open(request, timeout=_TIMEOUT) as response:
        body = response.read(_MAX_BODY_BYTES + 1)
    if len(body) > _MAX_BODY_BYTES:
        raise ValueError("cost report body exceeded the size ceiling")
    return json.loads(body.decode("utf-8", "replace"))


def fetch_buckets(starting_at: str, ending_at: str,
                  *, group_by: "tuple[str, ...]" = ("workspace_id",),
                  limit: int = 31) -> list:
    """Every time bucket in the window, following pagination.

    Raises on failure -- `compare()` is the caller that turns that into a
    summary. Kept raising here so a caller that WANTS the error can have it.
    """
    _name, raw = _raw_credential()
    if not raw:
        raise RuntimeError("no Anthropic admin credential in the environment")
    headers = _auth_headers(raw)

    params = [("starting_at", starting_at), ("ending_at", ending_at),
              ("bucket_width", "1d"), ("limit", str(max(1, min(31, limit))))]
    params += [("group_by[]", g) for g in group_by]

    buckets: list = []
    page: Optional[str] = None
    for _ in range(_MAX_PAGES):
        query = list(params) + ([("page", page)] if page else [])
        payload = _http_get_json(f"{COST_REPORT_URL}?{urlencode(query)}", headers)
        data = payload.get("data")
        if isinstance(data, list):
            buckets.extend(b for b in data if isinstance(b, dict))
        if not payload.get("has_more"):
            return buckets
        page = payload.get("next_page")
        if not isinstance(page, str) or not page:
            logger.warning("cost_report: has_more was true with no next_page -- "
                           "stopping with %d bucket(s)", len(buckets))
            return buckets
    logger.warning("cost_report: hit the %d-page ceiling -- the total below is "
                   "a FLOOR, not the full bill", _MAX_PAGES)
    return buckets


def total_usd(buckets: list, workspace_id: Optional[str] = None) -> "dict[str, Any]":
    """Sum a bucket list into dollars, optionally for one workspace only.

    `workspace_id` is `None` in the response for the DEFAULT workspace, which
    is not the same as "unknown" -- so the sentinel for "the default one" is
    the literal string `"default"`.
    """
    total, counted, skipped = 0.0, 0, 0
    by_workspace: "dict[str, float]" = {}
    for bucket in buckets:
        for item in (bucket.get("results") or []):
            if not isinstance(item, dict):
                continue
            owner = item.get("workspace_id") or "default"
            usd = _amount_to_usd(item.get("amount"))
            by_workspace[owner] = round(by_workspace.get(owner, 0.0) + usd, 6)
            if workspace_id is not None and owner != workspace_id:
                skipped += 1
                continue
            total += usd
            counted += 1
    return {"usd": round(total, 6), "items": counted,
            "items_other_workspace": skipped, "by_workspace": by_workspace}


# ---------------------------------------------------------------------------
# the comparison
# ---------------------------------------------------------------------------

def compare(*, workspace_id: Optional[str] = None,
            when: "date | None" = None) -> "dict[str, Any]":
    """Provider bill vs local ledger for the month to date.

    Never raises. On any failure the summary carries `error` and `ok: False`,
    because this runs from cron beside `budget --reconcile`.

    The verdict is deliberately conservative. With no workspace filter the
    provider figure covers the whole organization, so it is reported as
    context and `verdict` says it is not comparable. Only a workspace-scoped
    figure produces a real drift call.
    """
    from foundation.budget import load_budget  # noqa: WPS433 -- lazy, patchable

    workspace_id = workspace_id or (
        os.environ.get("ZUGAMIND_ANTHROPIC_WORKSPACE_ID") or "").strip() or None

    credential = describe_credential()
    starting_at, ending_at = month_window(when)
    summary: "dict[str, Any]" = {
        "ok": False,
        "credential": credential,
        "window": {"starting_at": starting_at, "ending_at": ending_at},
        "scope": "workspace" if workspace_id else "organization",
        "workspace_id": workspace_id,
    }

    if not credential["present"]:
        summary["error"] = credential["detail"]
        summary["verdict"] = "no credential -- nothing checked"
        return summary

    try:
        buckets = fetch_buckets(starting_at, ending_at)
    except urllib.error.HTTPError as exc:
        summary["error"] = f"HTTP {exc.code} from the cost report endpoint"
        summary["verdict"] = _explain_http(exc.code, credential)
        return summary
    except Exception as exc:  # noqa: BLE001 -- cron caller; report, never raise
        summary["error"] = str(exc)[:200]
        summary["verdict"] = "could not reach the cost report"
        return summary

    provider = total_usd(buckets, workspace_id)
    try:
        ledger = load_budget()
        local = float(ledger.get("paid_spent", ledger.get("spent", 0.0)) or 0.0)
        summary["month"] = ledger.get("month")
    except Exception as exc:  # noqa: BLE001
        summary["error"] = f"provider read fine; local ledger did not ({exc})"[:200]
        summary["provider_usd"] = provider["usd"]
        summary["verdict"] = "provider figure only -- no local number to compare"
        return summary

    summary.update({
        "ok": True,
        "provider_usd": provider["usd"],
        "ledger_usd": round(local, 6),
        "buckets": len(buckets),
        "by_workspace": provider["by_workspace"],
    })

    if workspace_id is None:
        summary["verdict"] = (
            "org-wide -- not comparable. This bill covers every key in the "
            "organization, not just this agent. Set "
            "ZUGAMIND_ANTHROPIC_WORKSPACE_ID to compare like for like.")
        return summary

    drift = round(provider["usd"] - local, 6)
    summary["drift_usd"] = drift
    summary["drift_pct"] = round(drift / local * 100.0, 2) if local > 0 else None
    if abs(drift) < 0.01:
        summary["verdict"] = "agrees with the bill"
    elif drift > 0:
        summary["verdict"] = (
            f"the ledger UNDER-counts by ${drift:.4f} -- the cap is looser than "
            "it looks. Check per-call cost estimates, and run "
            "`zugamind budget --dry-run` for spends that never got written.")
    else:
        summary["verdict"] = (
            f"the ledger OVER-counts by ${-drift:.4f} -- spending is being "
            "throttled earlier than the real bill requires (safe direction).")
    return summary


def _explain_http(code: int, credential: dict) -> str:
    """Turn a status code into the thing to actually do about it."""
    if code in (401, 403):
        if credential.get("kind") == "standard":
            return ("rejected -- this is a standard API key. The Admin API needs "
                    "an Admin key (sk-ant-admin01-...), an OAuth token with "
                    "org:admin, or a personal/service-account key that is not "
                    "workspace-scoped. Create one in Console > Settings > Admin "
                    "keys and set ANTHROPIC_ADMIN_KEY.")
        return ("rejected -- the credential is not authorised for the Admin API. "
                "Note the Admin API is unavailable to individual (non-"
                "organization) accounts.")
    if code == 404:
        return "endpoint not found -- check the account is a Console organization"
    if code == 429:
        return "rate limited -- this endpoint supports about one poll per minute"
    return f"HTTP {code}"


__all__ = ["compare", "describe_credential", "fetch_buckets", "total_usd",
           "month_window"]
