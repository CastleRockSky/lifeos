"""
data/maintenance_templates.py — Preset maintenance schedules.

Used by POST /api/vehicles/{id}/schedules/apply-template. Templates are
deliberately generic (no make/model specifics) — they're a starting point.
Each applied schedule is editable afterward; users can also add ad-hoc
schedules via the manual form.

When extending: keep intervals conservative (lean toward more frequent rather
than less). Each entry is a partial MaintenanceSchedule blob; vehicle_record_id
is filled in at apply time.
"""

from typing import Optional, TypedDict


class TemplateEntry(TypedDict, total=False):
    service_type: str
    interval_miles: Optional[int]
    interval_months: Optional[int]
    estimated_cost: Optional[float]
    notes: Optional[str]


class Template(TypedDict):
    label: str
    description: str
    schedules: list[TemplateEntry]


TEMPLATES: dict[str, Template] = {
    "standard_gas_passenger": {
        "label": "Standard gas car",
        "description": "Typical schedule for most gas passenger vehicles. Good default for sedans, SUVs, and minivans.",
        "schedules": [
            {"service_type": "Oil change",          "interval_miles": 5000,  "interval_months": 6,  "estimated_cost": 75},
            {"service_type": "Tire rotation",       "interval_miles": 7500,  "interval_months": 6,  "estimated_cost": 30},
            {"service_type": "Air filter",          "interval_miles": 20000, "interval_months": 24, "estimated_cost": 35},
            {"service_type": "Cabin air filter",    "interval_miles": 20000, "interval_months": 24, "estimated_cost": 40},
            {"service_type": "Brake inspection",    "interval_miles": 15000, "interval_months": 12, "estimated_cost": 0},
            {"service_type": "Brake fluid flush",   "interval_miles": 30000, "interval_months": 36, "estimated_cost": 100},
            {"service_type": "Coolant flush",       "interval_miles": 60000, "interval_months": 60, "estimated_cost": 150},
            {"service_type": "Transmission fluid",  "interval_miles": 60000, "interval_months": 60, "estimated_cost": 200},
            {"service_type": "Spark plugs",         "interval_miles": 60000, "interval_months": 60, "estimated_cost": 250},
            {"service_type": "Battery check",       "interval_miles": None,  "interval_months": 12, "estimated_cost": 0},
            {"service_type": "Registration renewal","interval_miles": None,  "interval_months": 12, "estimated_cost": None},
        ],
    },
    "high_mileage_truck": {
        "label": "Truck / heavy use",
        "description": "Shorter oil change interval, more frequent fluid services. Use for pickup trucks, towing, or heavy hauling.",
        "schedules": [
            {"service_type": "Oil change",          "interval_miles": 3500,  "interval_months": 4,  "estimated_cost": 90},
            {"service_type": "Tire rotation",       "interval_miles": 5000,  "interval_months": 4,  "estimated_cost": 35},
            {"service_type": "Air filter",          "interval_miles": 15000, "interval_months": 18, "estimated_cost": 40},
            {"service_type": "Fuel filter",         "interval_miles": 30000, "interval_months": 36, "estimated_cost": 60},
            {"service_type": "Brake inspection",    "interval_miles": 10000, "interval_months": 6,  "estimated_cost": 0},
            {"service_type": "Brake fluid flush",   "interval_miles": 25000, "interval_months": 30, "estimated_cost": 110},
            {"service_type": "Differential fluid",  "interval_miles": 30000, "interval_months": 36, "estimated_cost": 130},
            {"service_type": "Transfer case fluid", "interval_miles": 30000, "interval_months": 36, "estimated_cost": 110},
            {"service_type": "Coolant flush",       "interval_miles": 50000, "interval_months": 48, "estimated_cost": 160},
            {"service_type": "Transmission fluid",  "interval_miles": 45000, "interval_months": 48, "estimated_cost": 220},
            {"service_type": "Battery check",       "interval_miles": None,  "interval_months": 12, "estimated_cost": 0},
            {"service_type": "Registration renewal","interval_miles": None,  "interval_months": 12, "estimated_cost": None},
        ],
    },
    "hybrid": {
        "label": "Hybrid",
        "description": "Similar to gas but with battery checks and a longer brake interval (regen braking reduces wear).",
        "schedules": [
            {"service_type": "Oil change",          "interval_miles": 7500,  "interval_months": 12, "estimated_cost": 90},
            {"service_type": "Tire rotation",       "interval_miles": 7500,  "interval_months": 6,  "estimated_cost": 30},
            {"service_type": "Air filter",          "interval_miles": 20000, "interval_months": 24, "estimated_cost": 35},
            {"service_type": "Cabin air filter",    "interval_miles": 20000, "interval_months": 24, "estimated_cost": 40},
            {"service_type": "Brake inspection",    "interval_miles": 20000, "interval_months": 18, "estimated_cost": 0},
            {"service_type": "Hybrid battery check","interval_miles": 30000, "interval_months": 24, "estimated_cost": 100},
            {"service_type": "Inverter coolant",    "interval_miles": 100000,"interval_months": 120,"estimated_cost": 180},
            {"service_type": "Transmission fluid",  "interval_miles": 60000, "interval_months": 60, "estimated_cost": 200},
            {"service_type": "12V battery check",   "interval_miles": None,  "interval_months": 12, "estimated_cost": 0},
            {"service_type": "Registration renewal","interval_miles": None,  "interval_months": 12, "estimated_cost": None},
        ],
    },
    "ev": {
        "label": "EV (electric)",
        "description": "Minimal mechanical maintenance — mostly tires, brake fluid, and cabin filters.",
        "schedules": [
            {"service_type": "Tire rotation",       "interval_miles": 7500,  "interval_months": 6,  "estimated_cost": 30},
            {"service_type": "Cabin air filter",    "interval_miles": 20000, "interval_months": 24, "estimated_cost": 45},
            {"service_type": "Brake fluid flush",   "interval_miles": 30000, "interval_months": 36, "estimated_cost": 110},
            {"service_type": "Brake inspection",    "interval_miles": 25000, "interval_months": 24, "estimated_cost": 0,
             "notes": "EV regen braking dramatically extends pad life — inspect rather than swap."},
            {"service_type": "Battery coolant",     "interval_miles": None,  "interval_months": 48, "estimated_cost": 200},
            {"service_type": "Wiper blades",        "interval_miles": None,  "interval_months": 12, "estimated_cost": 35},
            {"service_type": "Registration renewal","interval_miles": None,  "interval_months": 12, "estimated_cost": None},
        ],
    },
    "minimal": {
        "label": "Minimal (oil + tires + registration)",
        "description": "Just the basics. Use when you don't want a long task list.",
        "schedules": [
            {"service_type": "Oil change",          "interval_miles": 5000,  "interval_months": 6,  "estimated_cost": 75},
            {"service_type": "Tire rotation",       "interval_miles": 7500,  "interval_months": 6,  "estimated_cost": 30},
            {"service_type": "Registration renewal","interval_miles": None,  "interval_months": 12, "estimated_cost": None},
        ],
    },
}


def list_templates_summary() -> list[dict]:
    """Light shape for the picker UI — label, description, count of entries."""
    return [
        {
            "key": k,
            "label": t["label"],
            "description": t["description"],
            "schedule_count": len(t["schedules"]),
        }
        for k, t in TEMPLATES.items()
    ]


def get_template(key: str) -> Optional[Template]:
    return TEMPLATES.get(key)
