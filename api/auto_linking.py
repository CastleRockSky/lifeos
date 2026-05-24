"""
auto_linking.py — Match auto-domain documents to vehicle records by VIN
(primary key) or year+make+model (fallback). Used by both the live
ingestion path and the one-shot Phase 6 backfill migration.

Lives in its own module to stay independent of routers/vehicles.py and
ingest.py — neither should import each other for this.
"""

from typing import Optional


def _normalise_vin(raw) -> Optional[str]:
    """VINs are 17 chars, uppercase A-Z and 0-9 (no I/O/Q). Be liberal in
    what we accept (lowercase, internal whitespace) and conservative in
    what we match (uppercase 17-char string)."""
    if not isinstance(raw, str):
        return None
    cleaned = "".join(ch for ch in raw.upper() if ch.isalnum())
    if len(cleaned) != 17:
        return None
    return cleaned


def _norm_str(raw) -> Optional[str]:
    if raw is None:
        return None
    return str(raw).strip().lower() or None


def _norm_year(raw) -> Optional[int]:
    """AI extraction sometimes returns 'vehicle_year' as a string ('2022')
    or sometimes as an int. Normalise to int for comparison."""
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return None


def _vehicle_keys(vehicle_data: dict) -> dict:
    """Pull the comparable fields off a vehicle's structured_records.data
    blob. Used to build a lookup index in the matcher."""
    return {
        "vin": _normalise_vin(vehicle_data.get("vin")),
        "year": _norm_year(vehicle_data.get("year")),
        "make": _norm_str(vehicle_data.get("make")),
        "model": _norm_str(vehicle_data.get("model")),
    }


_COMBINED_VEHICLE_KEYS = ("vehicle", "vehicle_description", "vehicle_name")


def _split_combined_vehicle(raw) -> tuple[Optional[str], Optional[str]]:
    """Service receipts often return the vehicle as a single combined
    string like "Toyota Sienna" or "2022 Toyota Sienna" instead of the
    split make/model fields that registrations use. Best-effort split:
    drop a leading 4-digit year if present, then take the first token as
    make and the rest as model. None,None on anything unparseable.
    """
    if not isinstance(raw, str):
        return None, None
    tokens = raw.strip().split()
    if not tokens:
        return None, None
    # Drop a leading 4-digit year (1900-2099) so "2022 Toyota Sienna" → "Toyota Sienna".
    if tokens[0].isdigit() and len(tokens[0]) == 4 and 1900 <= int(tokens[0]) <= 2099:
        tokens = tokens[1:]
    if len(tokens) < 2:
        return None, None
    return tokens[0], " ".join(tokens[1:])


def _doc_keys(extracted: dict) -> dict:
    """Pull the comparable fields off a document's ai_extracted_data blob.
    The AI prompt uses ``vehicle_year/make/model`` plus a top-level ``vin``;
    accept either ``vin`` or ``vehicle_vin`` so future prompt tweaks don't
    silently break linking. Falls back to splitting a combined ``vehicle``
    string ("Toyota Sienna") into make/model — common shape on service
    receipts, distinct from the explicit split on registrations.
    """
    make = extracted.get("vehicle_make") or extracted.get("make")
    model = extracted.get("vehicle_model") or extracted.get("model")
    if not make or not model:
        # Try the combined-field fallback.
        for key in _COMBINED_VEHICLE_KEYS:
            split_make, split_model = _split_combined_vehicle(extracted.get(key))
            if split_make and split_model:
                make = make or split_make
                model = model or split_model
                break
    return {
        "vin": _normalise_vin(extracted.get("vin") or extracted.get("vehicle_vin")),
        "year": _norm_year(extracted.get("vehicle_year") or extracted.get("year")),
        "make": _norm_str(make),
        "model": _norm_str(model),
    }


def is_phantom_vehicle(
    proposed: dict,
    existing_vehicles: list[dict],
) -> Optional[str]:
    """Sanity-check an AI-extracted ``vehicle`` blob before inserting it.

    Returns a short reason string if the record looks like a phantom (and
    should be skipped), or None to indicate it's safe to create. Real-world
    cases that motivated this:

    - The AI hallucinated a ``Volkswagen 2022`` vehicle out of a "Dual
      Registration Type" form field on a Sienna registration. The blob had
      ``model: null``. → Require model to be non-empty.
    - The AI re-extracted the same vehicle twice across different runs of
      the same registration doc. → No VIN + a y/m/m match against an
      existing active vehicle is almost certainly a duplicate.

    Caller still owns the decision (e.g. it might want to link the doc to
    the existing match instead of skipping silently); this helper just
    flags.
    """
    model = (proposed.get("model") or "").strip() if isinstance(proposed.get("model"), str) else None
    if not model:
        return "model field missing — likely hallucinated from a form-field misread"

    p_keys = _vehicle_keys(proposed)
    # If the proposed blob lacks a VIN AND triple-matches an existing active
    # vehicle, treat it as a duplicate.
    if not p_keys["vin"]:
        for v in existing_vehicles:
            e_keys = _vehicle_keys(v.get("data") or {})
            if (e_keys["year"] and e_keys["year"] == p_keys["year"]
                    and e_keys["make"] and e_keys["make"] == p_keys["make"]
                    and e_keys["model"] and e_keys["model"] == p_keys["model"]):
                return f"matches existing vehicle {v['id']} on year+make+model and has no VIN"

    return None


def is_vin_tail_mileage(mileage, vin) -> bool:
    """True when an extracted mileage value is suspiciously identical to the
    last 6 digits of a VIN. The AI conflates the two often enough (a 17-char
    VIN ending in digits looks just like a 6-digit odometer reading on
    receipts) that we strip the value when this matches and flag the doc
    for review. Compares the numeric value, so leading zeros line up either
    way."""
    if mileage is None or vin is None:
        return False
    try:
        m = int(mileage)
    except (TypeError, ValueError):
        return False
    tail = _normalise_vin(vin)
    if tail is None:
        return False
    last6 = tail[-6:]
    # Only digits in the last 6 count — a VIN can have letters there.
    if not last6.isdigit():
        return False
    return int(last6) == m


def match_document_to_vehicle(
    doc_extracted: dict,
    vehicles: list[dict],
) -> Optional[str]:
    """Given AI-extracted fields from a document and a list of candidate
    vehicles ({id, data}), return the id of the best match or None.

    Priority:
        1. VIN equality (any normalised non-empty match wins)
        2. year + make + model triple equality (all three must match)

    Ambiguity (e.g. two vehicles share a year/make/model) → None; the
    caller can decide whether to leave it unlinked or flag for review.
    """
    if not doc_extracted or not vehicles:
        return None

    d = _doc_keys(doc_extracted)

    if d["vin"]:
        for v in vehicles:
            k = _vehicle_keys(v.get("data") or {})
            if k["vin"] and k["vin"] == d["vin"]:
                return v["id"]

    # year+make+model triple. Require all three present on both sides.
    if d["year"] and d["make"] and d["model"]:
        candidates = []
        for v in vehicles:
            k = _vehicle_keys(v.get("data") or {})
            if (k["year"] == d["year"] and k["make"] == d["make"]
                    and k["model"] == d["model"]):
                candidates.append(v["id"])
        if len(candidates) == 1:
            return candidates[0]
        # 0 or 2+ — don't guess.

    return None
