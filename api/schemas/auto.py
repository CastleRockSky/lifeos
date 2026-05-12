"""
schemas/auto.py — Pydantic models for auto structured_records.

Shape definitions for record_type values: vehicle, maintenance_schedule,
service_record.
"""

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
    date: Optional[date] = None
    mileage: Optional[int] = None
    service_type: Optional[str] = None
    provider: Optional[str] = None
    cost: Optional[float] = None
    parts: list[str] = Field(default_factory=list)
    notes: Optional[str] = None
    document_id: Optional[str] = None
