"""
schemas/legal.py — Pydantic models for legal/identity structured_records.
"""

from datetime import date
from typing import Optional

from pydantic import BaseModel, ConfigDict


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)


class IdentityDocument(_Base):
    document_type: Optional[str] = None        # passport, drivers_license,
                                               # birth_certificate, marriage_certificate,
                                               # social_security_card, vehicle_title,
                                               # property_deed, will, trust, power_of_attorney
    issuing_authority: Optional[str] = None
    document_number_last4: Optional[str] = None
    issue_date: Optional[date] = None
    expiration_date: Optional[date] = None
    last_reviewed: Optional[date] = None
    storage_location: Optional[str] = None     # "home safe", "safety deposit box", etc.
    notes: Optional[str] = None


class LegalContact(_Base):
    name: Optional[str] = None
    specialty: Optional[str] = None            # estate planning, real estate, tax, etc.
    firm: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    last_consulted: Optional[date] = None
    notes: Optional[str] = None
