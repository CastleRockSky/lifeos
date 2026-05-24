"""
routers/trips.py — Business-mileage trips + IRS-rate reports
(Auto-redesign Phase 9).

Trips are stored as structured_records (record_type='business_trip').
The /report endpoint aggregates them by quarter and client and looks up
the IRS standard mileage rate for each trip's date. PDF export lives in
trips_pdf.py to keep the router thin.
"""

import json
import logging
import uuid
from datetime import date
from io import BytesIO
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel

from data.irs_mileage_rates import (
    DEFAULT_RATES, Rate, known_years, rate_blob_to_rate, rate_for_date,
)
from database import get_pool
from helpers import audit_log, get_user_email
from routers.vehicles import _data, _fetch_vehicle
from schemas import validate_record


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["trips"])


# ── Models ──────────────────────────────────────────────────────────────


class TripCreate(BaseModel):
    vehicle_record_id: str
    date: date
    miles: float
    purpose: str
    end_date: Optional[date] = None
    start_mileage: Optional[int] = None
    end_mileage: Optional[int] = None
    client: Optional[str] = None
    start_location: Optional[str] = None
    end_location: Optional[str] = None
    notes: Optional[str] = None
    is_round_trip: bool = True


class TripUpdate(BaseModel):
    date: Optional[date] = None
    end_date: Optional[date] = None
    miles: Optional[float] = None
    purpose: Optional[str] = None
    start_mileage: Optional[int] = None
    end_mileage: Optional[int] = None
    client: Optional[str] = None
    start_location: Optional[str] = None
    end_location: Optional[str] = None
    notes: Optional[str] = None
    is_round_trip: Optional[bool] = None


class RateUpsert(BaseModel):
    year: int
    rate: float
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    note: Optional[str] = None


# ── Helpers ─────────────────────────────────────────────────────────────


async def _fetch_trip(conn, tid: uuid.UUID):
    row = await conn.fetchrow(
        """SELECT id, record_type, subject_id, data
           FROM structured_records
           WHERE id = $1 AND deleted_at IS NULL""",
        tid,
    )
    if not row or row["record_type"] != "business_trip":
        raise HTTPException(404, "Trip not found")
    return row


def _check_odometer_discrepancy(start: Optional[int], end: Optional[int],
                                miles: Optional[float]) -> Optional[str]:
    """Return a warning string when entered miles disagrees with the
    odometer range by >10%, or None when there's nothing to flag. Pure."""
    if (not isinstance(start, int) or not isinstance(end, int)
            or not isinstance(miles, (int, float))):
        return None
    if end <= start:
        return None  # malformed; the schema doesn't enforce ordering
    delta = end - start
    if delta == 0:
        return None
    if abs(miles - delta) / delta > 0.10:
        return (f"Entered miles ({miles}) differs from odometer range "
                f"({delta} miles) by >10%.")
    return None


async def _load_rate_overrides(conn) -> list[Rate]:
    rows = await conn.fetch(
        """SELECT data FROM structured_records
           WHERE record_type = 'irs_mileage_rate' AND deleted_at IS NULL"""
    )
    out: list[Rate] = []
    for r in rows:
        blob = r["data"] if isinstance(r["data"], dict) else json.loads(r["data"])
        rate = rate_blob_to_rate(blob)
        if rate is not None:
            out.append(rate)
    return out


def _quarter_for(d: date) -> str:
    return f"Q{(d.month - 1) // 3 + 1}"


def _trip_date(blob: dict) -> Optional[date]:
    raw = blob.get("date")
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None
    return None


def compute_trip_report(
    trips: list[dict],
    overrides: list[Rate],
    tax_year: int,
    vehicle: Optional[dict] = None,
) -> dict:
    """Aggregate the trip list into the structure the report endpoint
    returns. Pure — no DB, no HTTP. Each trip's deduction is computed
    against the IRS rate that applied on its date (handles 2022's
    mid-year split correctly).

    Trips with no rate for their year contribute miles but zero deduction;
    the response carries a ``missing_rate_years`` list so the UI can
    prompt for input.
    """
    total_miles = 0.0
    total_deduction = 0.0
    by_quarter: dict[str, dict] = {}
    by_client: dict[str, dict] = {}
    missing_years: set[int] = set()
    enriched: list[dict] = []

    for t in trips:
        miles = t.get("miles")
        if not isinstance(miles, (int, float)):
            continue
        d = _trip_date(t)
        if d is None or d.year != tax_year:
            continue

        rate = rate_for_date(d, overrides=overrides)
        if rate is None:
            missing_years.add(d.year)
            deduction = 0.0
        else:
            deduction = float(miles) * rate

        total_miles += float(miles)
        total_deduction += deduction

        q = _quarter_for(d)
        bucket = by_quarter.setdefault(q, {"miles": 0.0, "deduction": 0.0})
        bucket["miles"] += float(miles)
        bucket["deduction"] += deduction

        client = (t.get("client") or "Unassigned").strip() or "Unassigned"
        cbucket = by_client.setdefault(client, {"miles": 0.0, "deduction": 0.0})
        cbucket["miles"] += float(miles)
        cbucket["deduction"] += deduction

        enriched.append({
            **t,
            "deduction": round(deduction, 2),
            "rate_used": rate,
        })

    # Sort trips chronologically for output; round the rollup totals.
    enriched.sort(key=lambda x: x.get("date") or "")
    return {
        "tax_year": tax_year,
        "vehicle": vehicle,
        "trip_count": len(enriched),
        "total_business_miles": round(total_miles, 2),
        "total_deduction": round(total_deduction, 2),
        "by_quarter": {q: {"miles": round(v["miles"], 2),
                           "deduction": round(v["deduction"], 2)}
                       for q, v in sorted(by_quarter.items())},
        "by_client": {c: {"miles": round(v["miles"], 2),
                          "deduction": round(v["deduction"], 2)}
                      for c, v in sorted(by_client.items(),
                                         key=lambda kv: -kv[1]["miles"])},
        "missing_rate_years": sorted(missing_years),
        "trips": enriched,
    }


# ── Trip CRUD ───────────────────────────────────────────────────────────


@router.post("/trips")
async def create_trip(body: TripCreate, request: Request):
    try:
        vid = uuid.UUID(body.vehicle_record_id)
    except ValueError:
        raise HTTPException(400, "Invalid vehicle_record_id")

    payload = body.model_dump(exclude_none=True)
    payload["tax_year"] = body.date.year
    cleaned = validate_record("business_trip", payload)

    pool = get_pool()
    async with pool.acquire() as conn:
        vehicle = await _fetch_vehicle(conn, vid)
        row = await conn.fetchrow(
            """INSERT INTO structured_records
                   (record_type, domain, subject_id, data)
               VALUES ('business_trip', 'auto', $1, $2)
               RETURNING *""",
            vehicle["subject_id"], cleaned,
        )

    warning = _check_odometer_discrepancy(body.start_mileage, body.end_mileage, body.miles)
    await audit_log(
        "create", get_user_email(request), "structured_records",
        str(row["id"]), {"record_type": "business_trip"},
    )
    return {"data": {"id": str(row["id"]), "data": cleaned, "warning": warning}}


@router.patch("/trips/{trip_id}")
async def update_trip(trip_id: str, body: TripUpdate, request: Request):
    try:
        tid = uuid.UUID(trip_id)
    except ValueError:
        raise HTTPException(400, "Invalid trip id")

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(400, "No fields to update")

    pool = get_pool()
    async with pool.acquire() as conn:
        rec = await _fetch_trip(conn, tid)
        merged = dict(_data(rec))
        merged.update(updates)
        if "date" in updates and isinstance(updates["date"], date):
            merged["tax_year"] = updates["date"].year
        cleaned = validate_record("business_trip", merged)
        await conn.execute(
            "UPDATE structured_records SET data = $1::jsonb WHERE id = $2",
            cleaned, tid,
        )

    warning = _check_odometer_discrepancy(
        cleaned.get("start_mileage"), cleaned.get("end_mileage"),
        cleaned.get("miles"),
    )
    await audit_log("update", get_user_email(request), "structured_records",
                    trip_id, {"fields": list(updates.keys())})
    return {"data": {"id": trip_id, "data": cleaned, "warning": warning}}


@router.delete("/trips/{trip_id}")
async def delete_trip(trip_id: str, request: Request):
    try:
        tid = uuid.UUID(trip_id)
    except ValueError:
        raise HTTPException(400, "Invalid trip id")

    pool = get_pool()
    async with pool.acquire() as conn:
        await _fetch_trip(conn, tid)
        await conn.execute("DELETE FROM structured_records WHERE id = $1", tid)

    await audit_log("delete", get_user_email(request), "structured_records",
                    trip_id, {"record_type": "business_trip"})
    return {"data": {"id": trip_id, "deleted": True}}


@router.get("/trips")
async def list_trips(
    vehicle_id: Optional[str] = Query(None),
    tax_year: Optional[int] = Query(None),
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
    per_page: int = Query(500, ge=1, le=2000),
):
    conditions = ["record_type = 'business_trip'", "deleted_at IS NULL"]
    params: list = []
    idx = 0
    if vehicle_id:
        idx += 1
        conditions.append(f"data->>'vehicle_record_id' = ${idx}")
        params.append(vehicle_id)
    if tax_year:
        idx += 1
        conditions.append(f"(data->>'tax_year')::int = ${idx}")
        params.append(tax_year)
    if from_:
        try:
            d = date.fromisoformat(from_)
        except ValueError:
            raise HTTPException(400, f"Invalid from date: {from_}")
        idx += 1
        conditions.append(f"(data->>'date')::date >= ${idx}")
        params.append(d)
    if to:
        try:
            d = date.fromisoformat(to)
        except ValueError:
            raise HTTPException(400, f"Invalid to date: {to}")
        idx += 1
        conditions.append(f"(data->>'date')::date <= ${idx}")
        params.append(d)

    params.append(per_page)
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT id, data, created_at FROM structured_records
                WHERE {' AND '.join(conditions)}
                ORDER BY data->>'date' DESC NULLS LAST, created_at DESC
                LIMIT ${idx + 1}""",
            *params,
        )

    return {"data": [
        {"id": str(r["id"]),
         "data": r["data"] if isinstance(r["data"], dict) else json.loads(r["data"]),
         "created_at": r["created_at"].isoformat()}
        for r in rows
    ]}


# ── Report ──────────────────────────────────────────────────────────────


async def _build_report(conn, tax_year: int, vehicle_id: Optional[str]) -> dict:
    """Shared report assembly used by both the JSON and PDF endpoints."""
    conditions = ["record_type = 'business_trip'", "deleted_at IS NULL",
                  "(data->>'tax_year')::int = $1"]
    params: list = [tax_year]
    if vehicle_id:
        conditions.append("data->>'vehicle_record_id' = $2")
        params.append(vehicle_id)

    rows = await conn.fetch(
        f"SELECT data FROM structured_records WHERE {' AND '.join(conditions)}",
        *params,
    )
    trips = [r["data"] if isinstance(r["data"], dict) else json.loads(r["data"])
             for r in rows]

    overrides = await _load_rate_overrides(conn)

    vehicle_blob: Optional[dict] = None
    if vehicle_id:
        try:
            vrow = await conn.fetchrow(
                """SELECT data FROM structured_records
                   WHERE id = $1 AND record_type = 'vehicle' AND deleted_at IS NULL""",
                uuid.UUID(vehicle_id),
            )
            if vrow:
                vd = vrow["data"] if isinstance(vrow["data"], dict) else json.loads(vrow["data"])
                vehicle_blob = {
                    "id": vehicle_id,
                    "year": vd.get("year"),
                    "make": vd.get("make"),
                    "model": vd.get("model"),
                    "vin": vd.get("vin"),
                }
        except ValueError:
            raise HTTPException(400, "Invalid vehicle_id")

    return compute_trip_report(trips, overrides, tax_year, vehicle_blob)


@router.get("/trips/report")
async def trip_report(
    tax_year: int = Query(...),
    vehicle_id: Optional[str] = Query(None),
):
    pool = get_pool()
    async with pool.acquire() as conn:
        report = await _build_report(conn, tax_year, vehicle_id)
    return {"data": report}


@router.get("/trips/report.pdf")
async def trip_report_pdf(
    tax_year: int = Query(...),
    vehicle_id: Optional[str] = Query(None),
):
    pool = get_pool()
    async with pool.acquire() as conn:
        report = await _build_report(conn, tax_year, vehicle_id)

    # Import locally so a missing reportlab doesn't block the JSON report.
    try:
        from trips_pdf import render_trip_report_pdf
    except ImportError as e:
        raise HTTPException(
            503, f"PDF export unavailable: reportlab not installed ({e})",
        )
    buf = BytesIO()
    render_trip_report_pdf(buf, report)
    buf.seek(0)
    filename = f"crs-mileage-{tax_year}"
    if vehicle_id:
        filename += f"-{vehicle_id[:8]}"
    filename += ".pdf"
    return Response(
        content=buf.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── IRS rate table ──────────────────────────────────────────────────────


@router.get("/irs-mileage-rates")
async def list_rates():
    """Combined view of defaults + user-added overrides."""
    pool = get_pool()
    async with pool.acquire() as conn:
        overrides = await _load_rate_overrides(conn)

    def _serialize(r: Rate) -> dict:
        return {
            "year": r.year, "rate": r.rate,
            "start_date": r.start_date.isoformat() if r.start_date else None,
            "end_date": r.end_date.isoformat() if r.end_date else None,
            "note": r.note,
        }

    return {"data": {
        "defaults": [_serialize(r) for r in DEFAULT_RATES],
        "overrides": [_serialize(r) for r in overrides],
        "known_years": sorted(known_years(overrides)),
    }}


@router.post("/irs-mileage-rates")
async def upsert_rate(body: RateUpsert, request: Request):
    """Add or update a user-defined IRS rate row. Used by the report flow
    when a tax year is missing — the UI prompts, posts the new rate here,
    re-runs the report."""
    if body.rate <= 0:
        raise HTTPException(400, "Rate must be positive")

    blob = body.model_dump(exclude_none=True)
    cleaned = validate_record("irs_mileage_rate", blob)

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO structured_records (record_type, domain, data)
               VALUES ('irs_mileage_rate', 'auto', $1)
               RETURNING *""",
            cleaned,
        )

    await audit_log("create", get_user_email(request), "structured_records",
                    str(row["id"]), {"record_type": "irs_mileage_rate",
                                     "year": body.year})
    return {"data": {"id": str(row["id"]), "data": cleaned}}
