"""The bill is the authority the local ledger cannot be.

budget.json is a tally of this agent's own estimates. cost_report asks the
provider what it actually charged. Three things make that easy to get wrong,
and each has a test here: `amount` is in CENTS, the report is ORG-WIDE, and
it needs an ADMIN credential rather than an API key.

Nothing in this file touches the network -- `_http_get_json` is replaced.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date

import pytest

import foundation.budget as budget
import foundation.cost_report as cost_report


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _bucket(*results) -> dict:
    return {"starting_at": "2026-08-01T00:00:00Z",
            "ending_at": "2026-08-02T00:00:00Z",
            "results": list(results)}


def _item(amount: str, workspace_id=None) -> dict:
    return {"amount": amount, "currency": "USD", "cost_type": "tokens",
            "workspace_id": workspace_id}


@pytest.fixture
def no_credential(monkeypatch):
    for name in cost_report._CREDENTIAL_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("ZUGAMIND_ANTHROPIC_WORKSPACE_ID", raising=False)


@pytest.fixture
def admin_key(monkeypatch, no_credential):
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "sk-ant-admin01-" + "x" * 80)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "engine" / "budget.json"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(budget, "BUDGET_FILE", path)
    monkeypatch.setattr(budget, "ENGINE_DIR", path.parent)
    path.write_text(json.dumps({
        "month": date.today().strftime("%Y-%m"), "spent": 2.0,
        "paid_spent": 2.0, "calls": {}, "remaining": 8.0,
    }))
    return path


def _serve(monkeypatch, *pages):
    """Replace the one HTTP call with a canned page sequence."""
    calls = []

    def fake(url, headers):
        calls.append(url)
        return pages[min(len(calls) - 1, len(pages) - 1)]

    monkeypatch.setattr(cost_report, "_http_get_json", fake)
    return calls


# ---------------------------------------------------------------------------
# 1. the cents trap
# ---------------------------------------------------------------------------

def test_amount_is_cents_not_dollars():
    """The API reference's own example: "123.78912" is $1.2378912.

    Reading it as dollars makes every figure 100x too large -- on the one
    number that gates spending.
    """
    assert cost_report._amount_to_usd("123.78912") == pytest.approx(1.2378912)
    assert cost_report._amount_to_usd("100") == pytest.approx(1.00)


def test_a_junk_amount_counts_as_zero_rather_than_exploding():
    assert cost_report._amount_to_usd(None) == 0.0
    assert cost_report._amount_to_usd("not-a-number") == 0.0


def test_the_total_is_in_dollars():
    total = cost_report.total_usd([_bucket(_item("250"), _item("125.5"))])
    assert total["usd"] == pytest.approx(3.755)
    assert total["items"] == 2


# ---------------------------------------------------------------------------
# 2. the credential is reported, never exposed
# ---------------------------------------------------------------------------

def test_no_credential_is_said_plainly(no_credential):
    described = cost_report.describe_credential()
    assert described["present"] is False and described["usable"] is False
    assert "ANTHROPIC_ADMIN_KEY" in described["detail"]


def test_an_admin_key_is_recognised_as_usable(admin_key):
    described = cost_report.describe_credential()
    assert described["kind"] == "admin" and described["usable"] is True
    assert described["name"] == "ANTHROPIC_ADMIN_KEY"


def test_a_standard_key_is_reported_as_maybe_not_a_flat_yes(monkeypatch,
                                                            no_credential):
    """A workspace-scoped key is refused by the Admin API; a personal one is
    not. The prefix cannot tell them apart, so neither does this."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-" + "y" * 80)
    described = cost_report.describe_credential()
    assert described["kind"] == "standard"
    assert described["usable"] is None, "a maybe must not be reported as a yes"


def test_describing_a_credential_never_exposes_it(monkeypatch, no_credential):
    """The whole reason this function exists instead of returning the key."""
    secret = "sk-ant-admin01-" + "z" * 80
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", secret)
    blob = json.dumps(cost_report.describe_credential())
    assert secret not in blob and "z" * 20 not in blob
    assert cost_report.describe_credential()["length"] == len(secret)


def test_an_oauth_token_goes_in_the_bearer_header_not_x_api_key():
    headers = cost_report._auth_headers("sk-ant-oat01-abc")
    assert headers["Authorization"].startswith("Bearer ")
    assert "x-api-key" not in headers


# ---------------------------------------------------------------------------
# 3. org-wide vs this agent
# ---------------------------------------------------------------------------

def test_without_a_workspace_it_refuses_to_call_a_drift(admin_key, ledger,
                                                        monkeypatch):
    """The org bill covers every key in the organization. Subtracting the
    local ledger from it produces a confident, meaningless number."""
    _serve(monkeypatch, {"data": [_bucket(_item("5000", "wrkspc_other"))],
                         "has_more": False})

    summary = cost_report.compare()

    assert summary["ok"] is True
    assert summary["scope"] == "organization"
    assert "drift_usd" not in summary, "an org total is not comparable"
    assert "not comparable" in summary["verdict"]


def test_with_a_workspace_the_drift_is_real(admin_key, ledger, monkeypatch):
    """$50.00 billed to us, $2.00 in the ledger -> the cap is 48 dollars looser
    than it looks."""
    _serve(monkeypatch, {"data": [_bucket(_item("5000", "wrkspc_mine"),
                                          _item("9999", "wrkspc_other"))],
                         "has_more": False})

    summary = cost_report.compare(workspace_id="wrkspc_mine")

    assert summary["provider_usd"] == pytest.approx(50.0)
    assert summary["ledger_usd"] == pytest.approx(2.0)
    assert summary["drift_usd"] == pytest.approx(48.0)
    assert "UNDER-counts" in summary["verdict"]


def test_over_counting_is_named_as_the_safe_direction(admin_key, ledger,
                                                      monkeypatch):
    _serve(monkeypatch, {"data": [_bucket(_item("50", "wrkspc_mine"))],
                         "has_more": False})

    summary = cost_report.compare(workspace_id="wrkspc_mine")

    assert summary["drift_usd"] == pytest.approx(-1.5)
    assert "OVER-counts" in summary["verdict"] and "safe" in summary["verdict"]


def test_agreement_is_reported_as_agreement(admin_key, ledger, monkeypatch):
    _serve(monkeypatch, {"data": [_bucket(_item("200", "wrkspc_mine"))],
                         "has_more": False})
    assert cost_report.compare(workspace_id="wrkspc_mine")["verdict"] == \
        "agrees with the bill"


def test_the_default_workspace_is_not_the_same_as_unknown():
    """`workspace_id` is null in the response for the DEFAULT workspace."""
    total = cost_report.total_usd([_bucket(_item("100", None))],
                                  workspace_id="default")
    assert total["usd"] == pytest.approx(1.0)


def test_the_workspace_can_come_from_the_environment(admin_key, ledger,
                                                     monkeypatch):
    monkeypatch.setenv("ZUGAMIND_ANTHROPIC_WORKSPACE_ID", "wrkspc_mine")
    _serve(monkeypatch, {"data": [_bucket(_item("200", "wrkspc_mine"))],
                         "has_more": False})
    assert cost_report.compare()["scope"] == "workspace"


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------

def test_pagination_follows_next_page(admin_key, monkeypatch):
    calls = _serve(
        monkeypatch,
        {"data": [_bucket(_item("100"))], "has_more": True, "next_page": "p2"},
        {"data": [_bucket(_item("100"))], "has_more": False},
    )
    buckets = cost_report.fetch_buckets("2026-08-01T00:00:00Z",
                                        "2026-08-29T23:59:59Z")
    assert len(buckets) == 2 and len(calls) == 2
    assert "page=p2" in calls[1]


def test_has_more_with_no_cursor_stops_instead_of_looping(admin_key, monkeypatch):
    """A server that says "more" and hands back no cursor must not spin."""
    calls = _serve(monkeypatch, {"data": [_bucket(_item("100"))],
                                 "has_more": True, "next_page": None})
    buckets = cost_report.fetch_buckets("2026-08-01T00:00:00Z",
                                        "2026-08-29T23:59:59Z")
    assert len(buckets) == 1 and len(calls) == 1


def test_the_page_ceiling_is_a_ceiling(admin_key, monkeypatch):
    calls = _serve(monkeypatch, {"data": [_bucket(_item("1"))],
                                 "has_more": True, "next_page": "loop"})
    cost_report.fetch_buckets("2026-08-01T00:00:00Z", "2026-08-29T23:59:59Z")
    assert len(calls) == cost_report._MAX_PAGES


def test_the_request_asks_for_daily_buckets_grouped_by_workspace(admin_key,
                                                                 monkeypatch):
    calls = _serve(monkeypatch, {"data": [], "has_more": False})
    cost_report.fetch_buckets("2026-08-01T00:00:00Z", "2026-08-29T23:59:59Z")
    assert "bucket_width=1d" in calls[0]
    assert "group_by%5B%5D=workspace_id" in calls[0]


def test_the_credential_is_never_put_in_the_url(admin_key, monkeypatch):
    """A key in a query string lands in every proxy and access log there is."""
    calls = _serve(monkeypatch, {"data": [], "has_more": False})
    cost_report.fetch_buckets("2026-08-01T00:00:00Z", "2026-08-29T23:59:59Z")
    assert "sk-ant" not in calls[0]


def test_a_redirect_on_a_credentialed_request_is_refused():
    """urllib copies headers -- credential included -- onto the redirect
    target with no host check."""
    handler = cost_report._NoRedirect()
    request = urllib.request.Request(cost_report.COST_REPORT_URL)
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(request, None, 302, "Found", {},
                                 "https://evil.example/collect")


# ---------------------------------------------------------------------------
# failure is reported, never raised
# ---------------------------------------------------------------------------

def test_no_credential_means_no_network_call_and_no_crash(no_credential,
                                                          monkeypatch):
    def explode(*a, **kw):
        raise AssertionError("must not reach the network with no credential")

    monkeypatch.setattr(cost_report, "_http_get_json", explode)

    summary = cost_report.compare()
    assert summary["ok"] is False and "nothing checked" in summary["verdict"]


def test_a_401_on_a_standard_key_says_what_to_do_about_it(monkeypatch,
                                                          no_credential, ledger):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-" + "y" * 80)

    def unauthorised(url, headers):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(cost_report, "_http_get_json", unauthorised)

    summary = cost_report.compare()
    assert summary["ok"] is False
    assert "Admin key" in summary["verdict"], summary["verdict"]


def test_a_network_failure_comes_back_as_a_summary_not_an_exception(
        admin_key, ledger, monkeypatch):
    """This runs from cron beside `budget --reconcile`."""
    def down(url, headers):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(cost_report, "_http_get_json", down)

    summary = cost_report.compare()
    assert summary["ok"] is False and summary["error"]
    assert "could not reach" in summary["verdict"]


def test_a_readable_bill_with_an_unreadable_ledger_still_reports_the_bill(
        admin_key, monkeypatch):
    _serve(monkeypatch, {"data": [_bucket(_item("100"))], "has_more": False})
    monkeypatch.setattr(cost_report, "compare", cost_report.compare)
    monkeypatch.setattr("foundation.budget.load_budget",
                        lambda: (_ for _ in ()).throw(OSError("disk wedged")))

    summary = cost_report.compare()
    assert summary["provider_usd"] == pytest.approx(1.0)
    assert "provider figure only" in summary["verdict"]


# ---------------------------------------------------------------------------
# the window
# ---------------------------------------------------------------------------

def test_the_window_is_the_month_to_date():
    """The ledger is month-keyed with no daily reset; the window has to match
    or the two numbers are not comparable at all."""
    start, end = cost_report.month_window(date(2026, 8, 29))
    assert start == "2026-08-01T00:00:00Z"
    assert end == "2026-08-29T23:59:59Z"
