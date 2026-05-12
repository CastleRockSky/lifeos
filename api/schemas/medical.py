"""
schemas/medical.py — Pydantic models for medical structured_records.

These define the shape of `structured_records.data` for record_type values:
provider, medication, condition, vaccination, lab_result_set.

All fields are optional so partial extraction (from a PDF that doesn't include
every detail) still yields a valid record. The schemas exist to:
  - reject obvious type mistakes (e.g. quantity as a string)
  - normalise field names
  - document the expected shape for HealthBot and the UI
"""

from datetime import date
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    # Allow extra fields so we don't lose AI-extracted data we haven't modelled yet.
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)


class Provider(_Base):
    name: Optional[str] = None
    specialty: Optional[str] = None
    practice: Optional[str] = None
    phone: Optional[str] = None
    fax: Optional[str] = None
    portal_url: Optional[str] = None
    address: Optional[str] = None
    npi: Optional[str] = None
    next_appointment: Optional[date] = None
    notes: Optional[str] = None


class Medication(_Base):
    name: Optional[str] = None
    dose: Optional[str] = None
    frequency: Optional[str] = None
    time_of_day: Optional[str] = None
    prescriber: Optional[str] = None
    pharmacy: Optional[str] = None
    rx_number: Optional[str] = None
    start_date: Optional[date] = None
    refill_date: Optional[date] = None
    quantity: Optional[int] = None
    refills_remaining: Optional[int] = None
    indication: Optional[str] = None
    status: Optional[str] = Field(default="active")  # active, discontinued, paused
    notes: Optional[str] = None


class Condition(_Base):
    name: Optional[str] = None
    icd10: Optional[str] = None
    diagnosed_date: Optional[date] = None
    diagnosing_provider: Optional[str] = None
    status: Optional[str] = Field(default="active")  # active, resolved, in_remission
    management: Optional[str] = None
    notes: Optional[str] = None


class Vaccination(_Base):
    name: Optional[str] = None
    date_administered: Optional[date] = None
    provider: Optional[str] = None
    lot_number: Optional[str] = None
    next_due: Optional[date] = None
    series: Optional[str] = None
    dose_number: Optional[int] = None


class LabResult(_Base):
    test: str
    value: Optional[float] = None
    value_text: Optional[str] = None  # for non-numeric ("positive", "trace")
    unit: Optional[str] = None
    reference_range: Optional[str] = None
    flag: Optional[str] = None  # normal, low, high, critical_low, critical_high


class LabResultSet(_Base):
    lab: Optional[str] = None
    ordering_provider: Optional[str] = None
    date: Optional[date] = None
    results: list[LabResult] = Field(default_factory=list)
