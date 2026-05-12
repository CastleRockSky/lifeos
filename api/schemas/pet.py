"""
schemas/pet.py — Pydantic models for pet (vet) structured_records.

Mirrors the medical schemas with pet-specific additions:
  - vet_provider: like provider, plus species_specialty
  - pet_medication: like medication, plus weight_based_dosing
  - pet_vaccination: like vaccination, plus required_for_boarding
  - pet_condition: same shape as condition
  - preventative_schedule: flea/tick, heartworm, dental cleaning intervals
"""

from datetime import date
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)


class VetProvider(_Base):
    name: Optional[str] = None
    practice: Optional[str] = None
    species_specialty: Optional[str] = None    # "small animal", "exotic", "avian", etc.
    phone: Optional[str] = None
    fax: Optional[str] = None
    portal_url: Optional[str] = None
    address: Optional[str] = None
    next_appointment: Optional[date] = None
    notes: Optional[str] = None


class PetMedication(_Base):
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
    weight_based_dosing: Optional[str] = None  # e.g. "0.5mg/kg"
    status: Optional[str] = Field(default="active")
    notes: Optional[str] = None


class PetVaccination(_Base):
    name: Optional[str] = None                 # Rabies, DHPP, Bordetella, etc.
    date_administered: Optional[date] = None
    provider: Optional[str] = None
    lot_number: Optional[str] = None
    next_due: Optional[date] = None
    series: Optional[str] = None
    dose_number: Optional[int] = None
    required_for_boarding: Optional[bool] = None
    notes: Optional[str] = None


class PetCondition(_Base):
    name: Optional[str] = None
    diagnosed_date: Optional[date] = None
    diagnosing_provider: Optional[str] = None
    status: Optional[str] = Field(default="active")
    management: Optional[str] = None
    notes: Optional[str] = None


class PreventativeSchedule(_Base):
    type: Optional[str] = None                 # flea_tick, heartworm, dental, deworming
    product: Optional[str] = None              # e.g. NexGard, Heartgard, Bravecto
    dose: Optional[str] = None
    frequency: Optional[str] = Field(default="monthly")  # monthly, quarterly, yearly
    last_administered: Optional[date] = None
    next_due: Optional[date] = None
    cost_per_dose: Optional[float] = None
    notes: Optional[str] = None
