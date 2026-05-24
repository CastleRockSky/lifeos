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


def _doc_keys(extracted: dict) -> dict:
    """Pull the comparable fields off a document's ai_extracted_data blob.
    The AI prompt uses ``vehicle_year/make/model`` plus a top-level ``vin``;
    accept either ``vin`` or ``vehicle_vin`` so future prompt tweaks don't
    silently break linking."""
    return {
        "vin": _normalise_vin(extracted.get("vin") or extracted.get("vehicle_vin")),
        "year": _norm_year(extracted.get("vehicle_year") or extracted.get("year")),
        "make": _norm_str(extracted.get("vehicle_make") or extracted.get("make")),
        "model": _norm_str(extracted.get("vehicle_model") or extracted.get("model")),
    }


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
