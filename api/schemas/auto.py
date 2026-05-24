"""
schemas/auto.py — Pydantic models for auto structured_records.

Shape definitions for record_type values: vehicle, maintenance_schedule,
service_record.
"""

import datetime
from datetime import date
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)


class Vehicle(_Base):
    year: Optional[int] = None
    make: Optional[str] = None
    model: Optional[str] = None
    trim: Optional[str] = None
    vin: Optional[str] = None
    license_plate: Optional[str] = None
    color: Optional[str] = None
    purchase_date: Optional[date] = None
    purchase_price: Optional[float] = None
    current_mileage: Optional[int] = None
    mileage_updated: Optional[date] = None
    registration_expiration: Optional[date] = None
    insurance_policy_id: Optional[str] = None
    loan_record_id: Optional[str] = None
    status: Optional[str] = Field(default="active")  # active, sold, totaled
    notes: Optional[str] = None


class MaintenanceSchedule(_Base):
    vehicle_record_id: Optional[str] = None
    service_type: Optional[str] = None
    interval_miles: Optional[int] = None
    interval_months: Optional[int] = None
    last_service_date: Optional[date] = None
    last_service_mileage: Optional[int] = None
    next_due_date: Optional[date] = None
    next_due_mileage: Optional[int] = None
    estimated_cost: Optional[float] = None
    provider: Optional[str] = None
    notes: Optional[str] = None


class ServiceRecord(_Base):
    vehicle_record_id: Optional[str] = None
    # `date` shadows the imported `date` type — pydantic resolves the bare
    # `Optional[date]` as None-only because the field name dominates the
    # local namespace. Use the qualified path to disambiguate.
    date: Optional[datetime.date] = None
    mileage: Optional[int] = None
    service_type: Optional[str] = None
    # Spec-defined enum; not enforced via Literal so legacy / external blobs
    # with novel category strings still round-trip without 422s.
    category: Optional[str] = Field(default="preventive")
    provider: Optional[str] = None
    cost: Optional[float] = None
    parts: list[str] = Field(default_factory=list)
    notes: Optional[str] = None
    document_id: Optional[str] = None


class VehicleRecall(_Base):
    """Open / acknowledged / resolved recall surfaced by the NHTSA VIN
    lookup. Distinct from the recall_notice document category, which is
    a physical letter that may or may not exist."""
    vehicle_record_id: Optional[str] = None
    nhtsa_campaign_number: Optional[str] = None  # unique per NHTSA campaign
    component: Optional[str] = None
    summary: Optional[str] = None
    consequence: Optional[str] = None
    remedy: Optional[str] = None
    report_received_date: Optional[date] = None
    # Lifecycle: open → acknowledged → resolved. (Spec-defined enum, but
    # kept as str to round-trip future NHTSA additions or manual states.)
    status: Optional[str] = Field(default="open")
    resolved_service_record_id: Optional[str] = None
    discovered_at: Optional[datetime.datetime] = None
    acknowledged_at: Optional[datetime.datetime] = None
    resolved_at: Optional[datetime.datetime] = None
    notes: Optional[str] = None
