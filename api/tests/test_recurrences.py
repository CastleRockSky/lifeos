"""Tests for recurrences.py — next-due-date computation for recurring records."""

from datetime import date, timedelta

import pytest

from recurrences import (
    _next_monthly_due,
    _parse_date,
    _next_due_for,
    _action_title_for,
    _DOMAIN_FOR_RECORD_TYPE,
)
from constants import DOMAINS


# ── _next_monthly_due ───────────────────────────────────────────────────

def test_monthly_due_this_month_when_day_not_yet_passed():
    assert _next_monthly_due(20, today=date(2026, 5, 16)) == date(2026, 5, 20)


def test_monthly_due_rolls_to_next_month_when_day_passed():
    assert _next_monthly_due(10, today=date(2026, 5, 16)) == date(2026, 6, 10)


def test_monthly_due_includes_today_itself():
    # "on or after today" — the due day landing exactly on today counts.
    assert _next_monthly_due(16, today=date(2026, 5, 16)) == date(2026, 5, 16)


def test_monthly_due_clamps_to_short_month():
    # Day 31 in a non-leap February clamps to the 28th, never crashes.
    assert _next_monthly_due(31, today=date(2026, 2, 1)) == date(2026, 2, 28)


def test_monthly_due_rolls_across_year_boundary():
    assert _next_monthly_due(10, today=date(2026, 12, 20)) == date(2027, 1, 10)


def test_monthly_due_clamps_out_of_range_day():
    # due_day 99 is clamped to 31 (then to the month's last day).
    assert _next_monthly_due(99, today=date(2026, 4, 1)) == date(2026, 4, 30)
    # due_day 0 is clamped up to 1.
    assert _next_monthly_due(0, today=date(2026, 4, 5)) == date(2026, 5, 1)


# ── _parse_date ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [None, "", 0, [], {}])
def test_parse_date_falsy_returns_none(value):
    assert _parse_date(value) is None


def test_parse_date_passes_through_date_object():
    d = date(2026, 1, 2)
    assert _parse_date(d) is d


def test_parse_date_parses_isoformat():
    assert _parse_date("2026-05-16") == date(2026, 5, 16)


@pytest.mark.parametrize("value", ["garbage", "2026-13-99", "16/05/2026"])
def test_parse_date_invalid_returns_none(value):
    assert _parse_date(value) is None


# ── _next_due_for ───────────────────────────────────────────────────────

def test_next_due_for_rejects_non_dict():
    assert _next_due_for("loan", None) is None
    assert _next_due_for("loan", "not-a-dict") is None


def test_next_due_for_unknown_record_type():
    assert _next_due_for("totally_unknown", {}) is None


def test_credit_account_future_due_date_is_returned():
    future = date.today() + timedelta(days=30)
    assert _next_due_for("credit_account", {"payment_due_date": future.isoformat()}) == future


def test_credit_account_past_due_date_is_dropped():
    past = date.today() - timedelta(days=30)
    assert _next_due_for("credit_account", {"payment_due_date": past.isoformat()}) is None


def test_loan_with_valid_due_day_returns_future_date():
    due = _next_due_for("loan", {"payment_due_day": 15})
    assert isinstance(due, date)
    assert due >= date.today()


def test_loan_without_due_day_returns_none():
    assert _next_due_for("loan", {}) is None
    assert _next_due_for("loan", {"payment_due_day": 0}) is None


def test_recurring_expense_monthly_vs_other_frequency():
    monthly = _next_due_for("recurring_expense", {"frequency": "monthly", "due_day": 5})
    assert isinstance(monthly, date)
    assert _next_due_for("recurring_expense", {"frequency": "yearly", "due_day": 5}) is None


def test_insurance_policy_auto_renew_suppresses_action():
    future = (date.today() + timedelta(days=10)).isoformat()
    assert _next_due_for(
        "insurance_policy", {"auto_renew": True, "expiration_date": future}
    ) is None


def test_tax_item_resolved_status_is_skipped():
    soon = (date.today() + timedelta(days=5)).isoformat()
    assert _next_due_for("tax_item", {"status": "paid", "due_date": soon}) is None
    assert _next_due_for("tax_item", {"status": "pending", "due_date": soon}) == date.fromisoformat(soon)


# ── _action_title_for ───────────────────────────────────────────────────

def test_action_title_credit_account():
    assert _action_title_for("credit_account", {"creditor": "Chase"}, 50.0) == "Pay Chase"
    assert _action_title_for("credit_account", {}, None) == "Pay credit card"


def test_action_title_tax_item_includes_year_when_present():
    assert _action_title_for(
        "tax_item", {"description": "Q2 estimate", "tax_year": 2025}, None
    ) == "Tax: Q2 estimate (TY 2025)"
    assert _action_title_for("tax_item", {}, None) == "Tax: tax item"


def test_action_title_unknown_record_type_falls_back():
    assert _action_title_for("mystery", {"name": "Foo"}, None) == "Foo"
    assert _action_title_for("mystery", {}, None) == "Action"


# ── _DOMAIN_FOR_RECORD_TYPE ─────────────────────────────────────────────

def test_every_record_type_maps_to_a_real_domain():
    for record_type, domain in _DOMAIN_FOR_RECORD_TYPE.items():
        assert domain in DOMAINS, f"{record_type} maps to unknown domain {domain!r}"
