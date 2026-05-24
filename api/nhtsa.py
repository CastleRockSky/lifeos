"""
nhtsa.py — Client for the NHTSA recalls API (free, no auth).

Two entry points:
- ``parse_nhtsa_results(payload, vehicle_record_id)``: pure helper that
  maps the API's ``{Results: [...]}`` shape to our internal
  ``vehicle_recall`` blob shape. Decoupled so it can be unit-tested
  without the network.
- ``fetch_recalls_for_vin(vin)``: async httpx call to the live API.

NHTSA endpoint:
  https://api.nhtsa.gov/recalls/recallsByVehicle?make=&model=&modelYear=

There is no documented VIN-specific endpoint in the public API; the spec
referenced one but it's not exposed under api.nhtsa.gov. We use the
make/model/year endpoint and rely on the caller knowing those fields.
"""

import logging
from datetime import date, datetime, timezone
from typing import Optional

import httpx


logger = logging.getLogger(__name__)


NHTSA_RECALLS_URL = "https://api.nhtsa.gov/recalls/recallsByVehicle"


def _parse_nhtsa_date(value) -> Optional[date]:
    """NHTSA dates arrive as ``DD/MM/YYYY`` (string) most of the time but
    sometimes as ``YYYY-MM-DD`` for newer entries. Tolerate both; return
    None on garbage."""
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    # ISO first (fast common case)
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    # MM/DD/YYYY or DD/MM/YYYY — NHTSA uses MM/DD/YYYY per their docs.
    parts = s.split("/")
    if len(parts) == 3:
        try:
            month, day, year = (int(p) for p in parts)
            return date(year, month, day)
        except (ValueError, TypeError):
            return None
    return None


def parse_nhtsa_results(payload: dict, vehicle_record_id: str) -> list[dict]:
    """Convert NHTSA's ``{Results: [...]}`` JSON into a list of
    vehicle_recall data blobs (one per recall). Always emits the
    fields needed for dedupe (``nhtsa_campaign_number``) and the
    fields the UI renders. ``discovered_at`` is set to "now" so a
    fresh fetch produces fresh timestamps.

    The function is forgiving — missing fields become None, never raise."""
    if not isinstance(payload, dict):
        return []
    # NHTSA's live API returns ``results`` (lowercase) but their docs
    # historically showed ``Results``. Accept either so the parser doesn't
    # silently drop everything if they flip back.
    results = payload.get("results")
    if not isinstance(results, list):
        results = payload.get("Results")
    if not isinstance(results, list):
        return []

    out: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for entry in results:
        if not isinstance(entry, dict):
            continue
        campaign = entry.get("NHTSACampaignNumber") or entry.get("nhtsa_campaign_number")
        if not campaign:
            # No campaign number = no way to dedupe. Skip rather than
            # creating a row that re-imports on every refresh.
            continue
        rrd = _parse_nhtsa_date(entry.get("ReportReceivedDate"))
        out.append({
            "vehicle_record_id": vehicle_record_id,
            "nhtsa_campaign_number": str(campaign).strip(),
            "component": (entry.get("Component") or "").strip() or None,
            "summary": (entry.get("Summary") or "").strip() or None,
            "consequence": (entry.get("Consequence") or "").strip() or None,
            "remedy": (entry.get("Remedy") or "").strip() or None,
            "report_received_date": rrd.isoformat() if rrd else None,
            "status": "open",
            "discovered_at": now_iso,
        })
    return out


def filter_new_recalls(parsed: list[dict], existing_campaigns: set[str]) -> list[dict]:
    """Given parsed recalls from NHTSA and the set of campaign numbers
    already on file for this vehicle, return only the ones we haven't
    seen. Pure — no DB access."""
    return [r for r in parsed
            if r.get("nhtsa_campaign_number")
            and r["nhtsa_campaign_number"] not in existing_campaigns]


async def fetch_recalls_for_vehicle(
    make: str, model: str, model_year: int, *, timeout: float = 15.0,
) -> dict:
    """Hit the live NHTSA API. Returns the raw payload dict on success;
    raises httpx.HTTPError on network/HTTP failures. Callers should treat
    this as best-effort and degrade gracefully when it fails (NHTSA is
    free and intermittently slow; never block a vehicle page on it)."""
    params = {"make": make, "model": model, "modelYear": str(model_year)}
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(NHTSA_RECALLS_URL, params=params)
        response.raise_for_status()
        return response.json()
