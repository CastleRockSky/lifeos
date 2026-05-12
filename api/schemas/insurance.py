"""
schemas/insurance.py — Pydantic models for insurance structured_records.
"""

from datetime import date
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)


class InsurancePolicy(_Base):
    carrier: Optional[str] = None
    policy_number: Optional[str] = None
    policy_type: Optional[str] = None          # auto, home, health, dental, vision,
                                               # life, pet, umbrella, disability, renters
    coverage_type: Optional[str] = None        # full, basic, hsa, ppo, etc.
    premium: Optional[float] = None            # plain "premium amount" — frequency below
    premium_monthly: Optional[float] = None    # convenience
    premium_frequency: Optional[str] = Field(default="monthly")  # monthly, quarterly, semiannual, yearly
    deductible: Optional[float] = None
    coverage_limits: Optional[dict] = None     # free-form per policy_type
    effective_date: Optional[date] = None
    expiration_date: Optional[date] = None
    auto_renew: Optional[bool] = None
    agent_name: Optional[str] = None
    agent_phone: Optional[str] = None
    agent_email: Optional[str] = None
    linked_domain: Optional[str] = None        # auto, home, vet, medical, etc.
    linked_record_id: Optional[str] = None     # specific vehicle/property/pet record
    status: Optional[str] = Field(default="active")  # active, lapsed, cancelled
    notes: Optional[str] = None
