"""
schemas/trips.py — Pydantic models for business-mileage tracking
(Auto-redesign Phase 9).

BusinessTrip captures one logged trip; IrsMileageRate is the user-added
override row format. Both are stored as structured_records.
"""

import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)


class BusinessTrip(_Base):
    """A single business-mileage trip. Personal trips are not logged here.

    ``miles`` is the source of truth for deduction calculations. ``start_mileage``
    / ``end_mileage`` are optional for cases where the user knows the
    odometer; the router warns when the two disagree by >10%. ``tax_year`` is
    redundant with ``date`` but persisted so filtered queries don't need to
    cast jsonb date strings every time.
    """
    vehicle_record_id: Optional[str] = None
    date: Optional[datetime.date] = None
    end_date: Optional[datetime.date] = None
    start_mileage: Optional[int] = None
    end_mileage: Optional[int] = None
    miles: Optional[float] = None
    purpose: Optional[str] = None
    client: Optional[str] = None
    start_location: Optional[str] = None
    end_location: Optional[str] = None
    notes: Optional[str] = None
    is_round_trip: Optional[bool] = Field(default=True)
    tax_year: Optional[int] = None


class IrsMileageRate(_Base):
    """User-added IRS mileage rate. The default table lives in
    ``data/irs_mileage_rates.py``; these rows merge on top so a brand-new
    tax year can be enabled without a code change."""
    year: Optional[int] = None
    rate: Optional[float] = None
    start_date: Optional[datetime.date] = None
    end_date: Optional[datetime.date] = None
    note: Optional[str] = None
