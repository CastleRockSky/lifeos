"""
schemas — Pydantic schemas for structured_records.data, keyed by record_type.

The router calls validate_record(record_type, data) on write. Unknown record
types are accepted as-is (so external integrations can store ad-hoc data),
but anything we have a schema for is validated and normalised.
"""

from typing import Any

from pydantic import ValidationError

from . import medical, financial, auto, home, pet


# record_type → Pydantic model
_REGISTRY: dict[str, Any] = {
    # Medical
    "provider": medical.Provider,
    "medication": medical.Medication,
    "condition": medical.Condition,
    "vaccination": medical.Vaccination,
    "lab_result_set": medical.LabResultSet,
    # Financial
    "bank_account": financial.BankAccount,
    "credit_account": financial.CreditAccount,
    "loan": financial.Loan,
    "recurring_expense": financial.RecurringExpense,
    "tax_item": financial.TaxItem,
    # Auto
    "vehicle": auto.Vehicle,
    "maintenance_schedule": auto.MaintenanceSchedule,
    "service_record": auto.ServiceRecord,
    # Home
    "property": home.Property,
    "appliance": home.Appliance,
    "contractor": home.Contractor,
    "home_maintenance_schedule": home.HomeMaintenanceSchedule,
    # Pet
    "vet_provider": pet.VetProvider,
    "pet_medication": pet.PetMedication,
    "pet_vaccination": pet.PetVaccination,
    "pet_condition": pet.PetCondition,
    "preventative_schedule": pet.PreventativeSchedule,
}


def known_record_types() -> list[str]:
    return sorted(_REGISTRY.keys())


def validate_record(record_type: str, data: dict) -> dict:
    """Validate `data` against the schema for `record_type`.

    Returns a normalised dict (extra keys are preserved). Raises pydantic
    ValidationError if the input fails schema constraints. Unknown record
    types are passed through unchanged.
    """
    model = _REGISTRY.get(record_type)
    if model is None:
        return data
    try:
        # `model_dump(exclude_none=False)` keeps null fields so callers can see
        # what was/wasn't extracted; downstream code drops them when storing.
        return model(**data).model_dump(exclude_none=False)
    except ValidationError:
        raise
