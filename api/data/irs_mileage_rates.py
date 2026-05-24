"""
data/irs_mileage_rates.py — IRS standard mileage rates for business use.

Source of truth for known years lives here; user-added overrides land in
``structured_records`` as ``record_type='irs_mileage_rate'`` and get merged
on top via ``rate_for_date(d, overrides=...)``. That way new tax years can
be added through the API without code changes, and the defaults survive a
container rebuild.

Verify each year against https://www.irs.gov/tax-professionals/standard-mileage-rates
before shipping it as a default.
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class Rate:
    """A single IRS rate window. start/end are inclusive; for years without a
    mid-year change either both are None (covers the whole year) or
    start_date=Jan 1, end_date=Dec 31 — handled identically by the lookup."""
    year: int
    rate: float
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    note: Optional[str] = None  # e.g. "IRS Notice 2022-13"


# Default table. The 2022 mid-year split is the only known case; the
# lookup is designed for it so future split years don't need code changes.
DEFAULT_RATES: list[Rate] = [
    Rate(2022, 0.585, date(2022, 1, 1), date(2022, 6, 30),
         note="IRS Notice 2022-3 (Jan–Jun)"),
    Rate(2022, 0.625, date(2022, 7, 1), date(2022, 12, 31),
         note="IRS Notice 2022-13 (Jul–Dec rate increase)"),
    Rate(2023, 0.655),
    Rate(2024, 0.670),
    Rate(2025, 0.700),
    # 2026 rate is published by IRS in late 2025; add when known.
]


def _covers(rate: Rate, d: date) -> bool:
    """Whether ``rate`` applies on date ``d``. A rate with no start/end
    covers the whole of its year."""
    if rate.year != d.year:
        return False
    if rate.start_date and d < rate.start_date:
        return False
    if rate.end_date and d > rate.end_date:
        return False
    return True


def rate_for_date(
    d: date,
    overrides: Optional[list[Rate]] = None,
) -> Optional[float]:
    """Return the IRS standard mileage rate (dollars per mile) that applies
    on ``d``, or None if no rate is known for that year.

    Overrides take precedence: callers pull user-added rates from
    structured_records, convert to ``Rate``, and pass them in. This keeps
    the helper pure (no DB dependency) while still letting the system
    persist new rates without a code change.
    """
    for r in (overrides or []):
        if _covers(r, d):
            return r.rate
    for r in DEFAULT_RATES:
        if _covers(r, d):
            return r.rate
    return None


def known_years(overrides: Optional[list[Rate]] = None) -> set[int]:
    """Convenience: every tax year that has at least one rate defined."""
    return {r.year for r in DEFAULT_RATES} | {r.year for r in (overrides or [])}


def rate_blob_to_rate(blob: dict) -> Optional[Rate]:
    """Coerce a structured_records ``data`` dict into a ``Rate``. Returns
    None when essential fields are missing rather than raising, since the
    caller is normally aggregating across multiple rows."""
    year = blob.get("year")
    rate = blob.get("rate")
    if not isinstance(year, int) or not isinstance(rate, (int, float)):
        return None

    def _d(raw):
        if raw is None or raw == "":
            return None
        if isinstance(raw, date):
            return raw
        try:
            return date.fromisoformat(raw)
        except (TypeError, ValueError):
            return None

    return Rate(
        year=year, rate=float(rate),
        start_date=_d(blob.get("start_date")),
        end_date=_d(blob.get("end_date")),
        note=blob.get("note") or None,
    )
