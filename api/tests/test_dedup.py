"""Tests for dedup.py — the date-aware duplicate filter."""

from datetime import date

from dedup import date_within_tolerance


def test_disabled_always_true():
    # tolerance_days < 0 disables the check entirely.
    assert date_within_tolerance(date(2026, 1, 1), date(2026, 12, 31), -1) is True


def test_missing_date_falls_through():
    # Can't compare if either date is absent — don't block on it.
    assert date_within_tolerance(None, date(2026, 1, 1), 5) is True
    assert date_within_tolerance(date(2026, 1, 1), None, 5) is True
    assert date_within_tolerance(None, None, 0) is True


def test_strict_requires_identical_dates():
    assert date_within_tolerance(date(2026, 1, 1), date(2026, 1, 1), 0) is True
    assert date_within_tolerance(date(2026, 1, 1), date(2026, 1, 2), 0) is False


def test_tolerance_window():
    d = date(2026, 5, 16)
    assert date_within_tolerance(d, date(2026, 5, 19), 5) is True    # 3 days apart
    assert date_within_tolerance(d, date(2026, 5, 11), 5) is True    # exactly 5 days
    assert date_within_tolerance(d, date(2026, 5, 10), 5) is False   # 6 days apart


def test_direction_does_not_matter():
    a, b = date(2026, 5, 16), date(2026, 5, 12)
    assert date_within_tolerance(a, b, 5) == date_within_tolerance(b, a, 5)


def test_monthly_statements_are_not_duplicates():
    # The motivating case: consecutive monthly statements look alike but
    # are ~a month apart, so the date filter must exclude them.
    assert date_within_tolerance(date(2026, 5, 1), date(2026, 4, 1), 5) is False
