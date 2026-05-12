"""
schemas/home.py — Pydantic models for home structured_records.

Shape definitions for record_type values: property, appliance, contractor,
home_maintenance_schedule.
"""

from datetime import date
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)


class Property(_Base):
    address: Optional[str] = None
    type: Optional[str] = None                # single_family, condo, townhouse, etc.
    year_built: Optional[int] = None
    sqft: Optional[int] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[float] = None
    hoa: Optional[bool] = None
    hoa_monthly: Optional[float] = None
    mortgage_record_id: Optional[str] = None
    insurance_policy_id: Optional[str] = None
    purchase_date: Optional[date] = None
    purchase_price: Optional[float] = None
    notes: Optional[str] = None


class Appliance(_Base):
    name: Optional[str] = None
    brand: Optional[str] = None
    model: Optional[str] = None
    serial: Optional[str] = None
    install_date: Optional[date] = None
    warranty_expiration: Optional[date] = None
    last_service: Optional[date] = None
    service_interval_months: Optional[int] = None
    next_service_due: Optional[date] = None
    contractor_record_id: Optional[str] = None
    location: Optional[str] = None             # "main floor", "garage", etc.
    notes: Optional[str] = None


class Contractor(_Base):
    name: Optional[str] = None
    trade: Optional[str] = None                # plumbing, electrical, hvac, landscaping, etc.
    phone: Optional[str] = None
    email: Optional[str] = None
    website: Optional[str] = None
    rating: Optional[int] = Field(default=None, ge=1, le=5)
    last_used: Optional[date] = None
    license_number: Optional[str] = None
    notes: Optional[str] = None


class HomeMaintenanceSchedule(_Base):
    task: Optional[str] = None
    interval_months: Optional[int] = None
    last_completed: Optional[date] = None
    next_due: Optional[date] = None
    estimated_cost: Optional[float] = None
    diy: Optional[bool] = None
    contractor_record_id: Optional[str] = None
    appliance_record_id: Optional[str] = None
    notes: Optional[str] = None
