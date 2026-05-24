"""
routers/vehicles.py — Vehicle-specific endpoints.

Vehicles themselves are structured_records (record_type='vehicle'); the
generic /api/records endpoints will still work for them. This router holds
the vehicle-specific operations:
  - Mileage logging + maintenance recompute (Phase 7 of the broader spec)
  - Vehicle CRUD wrappers, archive, merge (Auto-redesign Phase 2)
"""

import json
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from database import get_pool
from helpers import audit_log, get_user_email
from recurrences import ensure_recurring_action_item
from schemas import validate_record
from data.maintenance_templates import TEMPLATES, list_templates_summary, get_template

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vehicles", tags=["vehicles"])

# Within this many miles of next_due_mileage we'll create an action item.
_MILEAGE_DUE_WINDOW = 500

# Status lifecycle values. `merged` records exist purely as audit trail and
# must never appear in any UI list — filter them out everywhere.
VEHICLE_STATUSES = {"active", "sold", "totaled", "archived", "merged"}


class MileageUpdate(BaseModel):
    mileage: int
    date: Optional[date] = None
    notes: Optional[str] = None


class VehicleCreate(BaseModel):
    # Required identity fields; everything else can be added later via PATCH.
    year: int
    make: str
    model: str
    # Common optional fields surfaced in the Add drawer.
    trim: Optional[str] = None
    vin: Optional[str] = None
    license_plate: Optional[str] = None
    color: Optional[str] = None
    current_mileage: Optional[int] = None
    purchase_date: Optional[date] = None
    purchase_price: Optional[float] = None
    registration_expiration: Optional[date] = None
    notes: Optional[str] = None
    subject_id: Optional[str] = None


class VehicleUpdate(BaseModel):
    # Any subset of Vehicle fields. Sent as a partial patch; only fields
    # explicitly included are touched. Pass null to clear a field.
    year: Optional[int] = None
    make: Optional[str] = None
    model: Optional[str] = None
    trim: Optional[str] = None
    vin: Optional[str] = None
    license_plate: Optional[str] = None
    color: Optional[str] = None
    current_mileage: Optional[int] = None
    purchase_date: Optional[date] = None
    purchase_price: Optional[float] = None
    registration_expiration: Optional[date] = None
    insurance_policy_id: Optional[str] = None
    loan_record_id: Optional[str] = None
    status: Optional[str] = None
    disposed_date: Optional[date] = None
    notes: Optional[str] = None
    # Use this sentinel to distinguish "field not in payload" (no-op)
    # from "field present but null" (clear) — Pydantic v2 sets a flag in
    # model_fields_set we use below.

    class Config:
        # Allow null clears.
        extra = "forbid"


class VehicleArchive(BaseModel):
    disposed_date: Optional[date] = None
    new_status: str = "archived"  # one of: archived, sold, totaled


class VehicleMerge(BaseModel):
    source_vehicle_id: str
    target_vehicle_id: str
    # For any field listed here, take the value from this side. Anything not
    # listed keeps the target's value. Special value 'concat' valid only for
    # 'notes' — appends source notes to target.
    field_resolutions: dict = {}


def _data(row) -> dict:
    return row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])


async def _fetch_vehicle(conn, vid: uuid.UUID):
    row = await conn.fetchrow(
        """SELECT id, record_type, subject_id, data
           FROM structured_records
           WHERE id = $1 AND deleted_at IS NULL""",
        vid,
    )
    if not row:
        raise HTTPException(404, "Vehicle not found")
    if row["record_type"] != "vehicle":
        raise HTTPException(400, "Record is not a vehicle")
    return row


@router.post("/{vehicle_id}/mileage")
async def log_mileage(vehicle_id: str, body: MileageUpdate, request: Request):
    """Update a vehicle's current mileage, write a metric, and recompute
    maintenance dues. If any service is due within 500 miles or past due,
    create an action item."""
    try:
        vid = uuid.UUID(vehicle_id)
    except ValueError:
        raise HTTPException(400, "Invalid vehicle id")

    if body.mileage <= 0:
        raise HTTPException(400, "Mileage must be positive")

    update_date = body.date or date.today()
    pool = get_pool()

    async with pool.acquire() as conn:
        vehicle = await conn.fetchrow("""
            SELECT id, record_type, subject_id, data
            FROM structured_records
            WHERE id = $1 AND deleted_at IS NULL
        """, vid)
        if not vehicle:
            raise HTTPException(404, "Vehicle not found")
        if vehicle["record_type"] != "vehicle":
            raise HTTPException(400, "Record is not a vehicle")

        vdata = _data(vehicle)
        prior_mileage = vdata.get("current_mileage")
        if isinstance(prior_mileage, int) and body.mileage < prior_mileage:
            raise HTTPException(
                400,
                f"New mileage ({body.mileage}) is lower than current ({prior_mileage}). "
                "Use a separate correction flow if this is intentional.",
            )

        vdata["current_mileage"] = body.mileage
        vdata["mileage_updated"] = update_date.isoformat()

        await conn.execute(
            "UPDATE structured_records SET data = $1::jsonb WHERE id = $2",
            vdata, vid,
        )

        # Trend snapshot
        if vehicle["subject_id"]:
            await conn.execute("""
                INSERT INTO time_series_metrics
                    (subject_id, metric_type, value_numeric, recorded_at, source, notes)
                VALUES ($1, 'mileage', $2, $3, 'manual', $4)
            """,
                vehicle["subject_id"],
                body.mileage,
                datetime.combine(update_date, datetime.min.time(), tzinfo=timezone.utc),
                body.notes,
            )

        actions_created = await _recompute_schedules(conn, vid, body.mileage, update_date)

    await audit_log("update", get_user_email(request), "structured_records",
                    vehicle_id, {"mileage": body.mileage})

    return {
        "data": {
            "vehicle_id": vehicle_id,
            "mileage": body.mileage,
            "mileage_updated": update_date.isoformat(),
            "actions_created": actions_created,
        }
    }


# ── Vehicle CRUD (Auto-redesign Phase 2) ────────────────────────────────

@router.post("")
async def create_vehicle(body: VehicleCreate, request: Request):
    """Create a vehicle structured_record. Thin wrapper that hardcodes
    record_type=vehicle, domain=auto, status=active."""
    payload = body.model_dump(exclude_none=True)
    subject_id = payload.pop("subject_id", None)
    payload.setdefault("status", "active")

    # Validate against the Vehicle schema so future fields stay consistent.
    cleaned = validate_record("vehicle", payload)

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO structured_records
                   (record_type, domain, subject_id, data)
               VALUES ('vehicle', 'auto', $1, $2)
               RETURNING *""",
            uuid.UUID(subject_id) if subject_id else None,
            cleaned,
        )

    await audit_log("create", get_user_email(request), "structured_records",
                    str(row["id"]), {"record_type": "vehicle"})
    return {"data": {"id": str(row["id"]), "data": cleaned}}


@router.patch("/{vehicle_id}")
async def update_vehicle(vehicle_id: str, body: VehicleUpdate, request: Request):
    """Partial update of any Vehicle field. Pass null to clear a field."""
    try:
        vid = uuid.UUID(vehicle_id)
    except ValueError:
        raise HTTPException(400, "Invalid vehicle id")

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(400, "No fields to update")

    if "status" in updates and updates["status"] not in VEHICLE_STATUSES:
        raise HTTPException(400, f"Invalid status: {updates['status']}")

    pool = get_pool()
    async with pool.acquire() as conn:
        vehicle = await _fetch_vehicle(conn, vid)
        merged = dict(_data(vehicle))
        merged.update(updates)
        cleaned = validate_record("vehicle", merged)
        await conn.execute(
            "UPDATE structured_records SET data = $1::jsonb WHERE id = $2",
            cleaned, vid,
        )

    await audit_log("update", get_user_email(request), "structured_records",
                    vehicle_id, {"fields": list(updates.keys())})
    return {"data": {"id": vehicle_id, "data": cleaned}}


@router.post("/{vehicle_id}/archive")
async def archive_vehicle(vehicle_id: str, body: VehicleArchive, request: Request):
    """Set status to archived (or sold/totaled). Soft delete — never removes
    the row, so service history is preserved."""
    if body.new_status not in {"archived", "sold", "totaled"}:
        raise HTTPException(400, f"Invalid archive status: {body.new_status}")

    try:
        vid = uuid.UUID(vehicle_id)
    except ValueError:
        raise HTTPException(400, "Invalid vehicle id")

    pool = get_pool()
    async with pool.acquire() as conn:
        vehicle = await _fetch_vehicle(conn, vid)
        merged = dict(_data(vehicle))
        merged["status"] = body.new_status
        if body.disposed_date and body.new_status in {"sold", "totaled"}:
            merged["disposed_date"] = body.disposed_date.isoformat()
        cleaned = validate_record("vehicle", merged)
        await conn.execute(
            "UPDATE structured_records SET data = $1::jsonb WHERE id = $2",
            cleaned, vid,
        )

    await audit_log("archive", get_user_email(request), "structured_records",
                    vehicle_id, {"new_status": body.new_status})
    return {"data": {"id": vehicle_id, "status": body.new_status}}


# ── Merge ───────────────────────────────────────────────────────────────

async def _count_dependents(conn, vehicle_id: str) -> dict:
    """Count records that would move if this vehicle were merged into another."""
    schedule_count = await conn.fetchval(
        """SELECT COUNT(*) FROM structured_records
           WHERE deleted_at IS NULL
             AND record_type = 'maintenance_schedule'
             AND data->>'vehicle_record_id' = $1""",
        vehicle_id,
    )
    service_count = await conn.fetchval(
        """SELECT COUNT(*) FROM structured_records
           WHERE deleted_at IS NULL
             AND record_type = 'service_record'
             AND data->>'vehicle_record_id' = $1""",
        vehicle_id,
    )
    # action_items follow schedules via source_record_id, so a separate count
    # is informational: how many open actions point at THIS vehicle's schedules.
    action_count = await conn.fetchval(
        """SELECT COUNT(*) FROM action_items
           WHERE deleted_at IS NULL
             AND source_record_id IN (
                 SELECT id FROM structured_records
                 WHERE deleted_at IS NULL
                   AND record_type = 'maintenance_schedule'
                   AND data->>'vehicle_record_id' = $1)""",
        vehicle_id,
    )
    return {
        "maintenance_schedules": schedule_count or 0,
        "service_records": service_count or 0,
        "action_items": action_count or 0,
    }


@router.get("/{vehicle_id}/merge-preview")
async def merge_preview(vehicle_id: str):
    """Counts of what would move if this vehicle were merged into another.
    Lets the UI render an honest "this will move N things" summary."""
    try:
        uuid.UUID(vehicle_id)
    except ValueError:
        raise HTTPException(400, "Invalid vehicle id")

    pool = get_pool()
    async with pool.acquire() as conn:
        await _fetch_vehicle(conn, uuid.UUID(vehicle_id))
        counts = await _count_dependents(conn, vehicle_id)
    return {"data": counts}


@router.post("/merge")
async def merge_vehicles(body: VehicleMerge, request: Request):
    """Merge source vehicle into target.

    Reassigns vehicle_record_id on dependent service_records and
    maintenance_schedules. Action items follow schedules implicitly (their
    source_record_id still points at the same schedule row, which now has a
    different vehicle_record_id). Applies field_resolutions to the target's
    data blob and soft-deletes the source with status='merged' and
    merged_into_vehicle_id set.

    Note: time_series_metrics mileage rows are keyed on subject_id (not
    vehicle_id), so they aren't reassigned. If two vehicles share a subject,
    mileage history was already commingled before the merge.
    """
    if body.source_vehicle_id == body.target_vehicle_id:
        raise HTTPException(400, "Cannot merge a vehicle into itself")

    try:
        src_id = uuid.UUID(body.source_vehicle_id)
        tgt_id = uuid.UUID(body.target_vehicle_id)
    except ValueError:
        raise HTTPException(400, "Invalid vehicle id")

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            source = await _fetch_vehicle(conn, src_id)
            target = await _fetch_vehicle(conn, tgt_id)

            src_data = _data(source)
            tgt_data = dict(_data(target))

            if src_data.get("status") in ("merged", "archived"):
                raise HTTPException(
                    400,
                    f"Source vehicle is already {src_data.get('status')} — cannot merge",
                )
            if tgt_data.get("status") in ("merged", "archived"):
                raise HTTPException(
                    400,
                    f"Target vehicle is {tgt_data.get('status')} — cannot merge into it",
                )

            # Apply field resolutions. Default = keep target's value.
            for field, resolution in (body.field_resolutions or {}).items():
                if field in ("status", "merged_into_vehicle_id"):
                    # These are merge-machinery fields; ignore any override.
                    continue
                if resolution == "source":
                    tgt_data[field] = src_data.get(field)
                elif resolution == "target":
                    pass  # explicit keep
                elif resolution == "concat" and field == "notes":
                    src_n, tgt_n = src_data.get("notes"), tgt_data.get("notes")
                    if src_n and tgt_n:
                        tgt_data["notes"] = f"{tgt_n}\n\n--- Merged from source ---\n{src_n}"
                    elif src_n:
                        tgt_data["notes"] = src_n
                else:
                    raise HTTPException(
                        400,
                        f"Invalid resolution for {field}: {resolution} "
                        "(use 'source', 'target', or 'concat' for notes)",
                    )

            cleaned_target = validate_record("vehicle", tgt_data)

            # Reassign dependents. The data->>'vehicle_record_id' string
            # comparison is correct because the column is stored as text.
            sched_moved = await conn.execute(
                """UPDATE structured_records
                   SET data = jsonb_set(data, '{vehicle_record_id}', to_jsonb($2::text))
                   WHERE deleted_at IS NULL
                     AND record_type = 'maintenance_schedule'
                     AND data->>'vehicle_record_id' = $1""",
                str(src_id), str(tgt_id),
            )
            svc_moved = await conn.execute(
                """UPDATE structured_records
                   SET data = jsonb_set(data, '{vehicle_record_id}', to_jsonb($2::text))
                   WHERE deleted_at IS NULL
                     AND record_type = 'service_record'
                     AND data->>'vehicle_record_id' = $1""",
                str(src_id), str(tgt_id),
            )

            # Write target's resolved data.
            await conn.execute(
                "UPDATE structured_records SET data = $1::jsonb WHERE id = $2",
                cleaned_target, tgt_id,
            )

            # Soft-delete source.
            src_data_final = dict(src_data)
            src_data_final["status"] = "merged"
            src_data_final["merged_into_vehicle_id"] = str(tgt_id)
            cleaned_source = validate_record("vehicle", src_data_final)
            await conn.execute(
                "UPDATE structured_records SET data = $1::jsonb WHERE id = $2",
                cleaned_source, src_id,
            )

    await audit_log(
        "merge", get_user_email(request), "structured_records",
        str(tgt_id),
        {"source_vehicle_id": str(src_id), "target_vehicle_id": str(tgt_id)},
    )

    return {
        "data": {
            "source_vehicle_id": str(src_id),
            "target_vehicle_id": str(tgt_id),
            "schedules_moved": _parse_update_count(sched_moved),
            "services_moved": _parse_update_count(svc_moved),
        }
    }


def _parse_update_count(result: str) -> int:
    # asyncpg returns 'UPDATE N' from execute() on UPDATEs.
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


# ── Maintenance schedule templates (Auto-redesign Phase 3) ──────────────

class ApplyTemplate(BaseModel):
    template_key: str
    skip_duplicates: bool = True
    # Optional: limit to a subset of the template's entries (by service_type).
    # When None, every entry in the template is applied.
    only_service_types: Optional[list[str]] = None


@router.post("/{vehicle_id}/schedules/apply-template", tags=["maintenance"])
async def apply_template(vehicle_id: str, body: ApplyTemplate, request: Request):
    """Create maintenance_schedule records for each entry in the named template.

    Skips entries whose service_type already exists for this vehicle when
    skip_duplicates is True (the default). After creation, recomputes any
    schedules so action items fire for things that are immediately due.
    """
    try:
        vid = uuid.UUID(vehicle_id)
    except ValueError:
        raise HTTPException(400, "Invalid vehicle id")

    tpl = get_template(body.template_key)
    if not tpl:
        raise HTTPException(404, f"Template not found: {body.template_key}")

    pool = get_pool()
    async with pool.acquire() as conn:
        vehicle = await _fetch_vehicle(conn, vid)

        existing_types: set[str] = set()
        if body.skip_duplicates:
            rows = await conn.fetch(
                """SELECT data->>'service_type' AS svc
                   FROM structured_records
                   WHERE deleted_at IS NULL
                     AND record_type = 'maintenance_schedule'
                     AND data->>'vehicle_record_id' = $1""",
                str(vid),
            )
            existing_types = {r["svc"] for r in rows if r["svc"]}

        wanted = body.only_service_types
        entries = tpl["schedules"]
        if wanted is not None:
            wanted_set = set(wanted)
            entries = [e for e in entries if e.get("service_type") in wanted_set]

        # Seed last_service_* to "right now" so each schedule has a baseline
        # the recompute can project from. Without this, next_due_* stays null
        # and nothing in the schedule / timeline / action engine works until
        # the user edits every entry by hand. Conservative side-effect: no
        # alerts fire at apply time (first interval starts fresh).
        vdata = vehicle["data"] if isinstance(vehicle["data"], dict) else json.loads(vehicle["data"])
        current_mileage = vdata.get("current_mileage")
        today = date.today()
        today_iso = today.isoformat()

        created: list[str] = []
        skipped: list[str] = []
        for entry in entries:
            svc = entry.get("service_type")
            if not svc:
                continue
            if svc in existing_types:
                skipped.append(svc)
                continue
            data = {**entry, "vehicle_record_id": str(vid)}
            if entry.get("interval_miles") and current_mileage is not None:
                data.setdefault("last_service_mileage", current_mileage)
            if entry.get("interval_months"):
                data.setdefault("last_service_date", today_iso)
            cleaned = validate_record("maintenance_schedule", data)
            row = await conn.fetchrow(
                """INSERT INTO structured_records
                       (record_type, domain, subject_id, data)
                   VALUES ('maintenance_schedule', 'auto', $1, $2)
                   RETURNING id""",
                vehicle["subject_id"],
                cleaned,
            )
            created.append(str(row["id"]))

        # Recompute populates next_due_mileage, next_due_date, and
        # predicted_due_date so the timeline + action-item engine have data
        # immediately rather than waiting for the next mileage log.
        if current_mileage is not None:
            await _recompute_schedules(conn, vid, current_mileage, today)

    await audit_log(
        "apply_template", get_user_email(request), "structured_records",
        vehicle_id, {"template_key": body.template_key, "created": len(created), "skipped": len(skipped)},
    )
    return {"data": {"created": created, "skipped": skipped, "template_key": body.template_key}}


# ── Maintenance schedule CRUD (Auto-redesign Phase 3) ───────────────────

class ScheduleCreate(BaseModel):
    vehicle_record_id: str
    service_type: str
    interval_miles: Optional[int] = None
    interval_months: Optional[int] = None
    last_service_date: Optional[date] = None
    last_service_mileage: Optional[int] = None
    estimated_cost: Optional[float] = None
    provider: Optional[str] = None
    notes: Optional[str] = None


class ScheduleUpdate(BaseModel):
    service_type: Optional[str] = None
    interval_miles: Optional[int] = None
    interval_months: Optional[int] = None
    last_service_date: Optional[date] = None
    last_service_mileage: Optional[int] = None
    estimated_cost: Optional[float] = None
    provider: Optional[str] = None
    notes: Optional[str] = None


async def _fetch_schedule(conn, sid: uuid.UUID):
    row = await conn.fetchrow(
        """SELECT id, record_type, subject_id, data
           FROM structured_records
           WHERE id = $1 AND deleted_at IS NULL""",
        sid,
    )
    if not row or row["record_type"] != "maintenance_schedule":
        raise HTTPException(404, "Schedule not found")
    return row


def _mpd_from_metrics(metrics) -> Optional[float]:
    """Miles-per-day cadence from a chronologically-ordered list of mileage
    metrics. Each element must support ``["value_numeric"]`` and
    ``["recorded_at"]`` access (asyncpg Records and plain dicts both work).

    Returns None when there aren't ≥2 readings, the span is <7 days, or the
    odometer didn't increase — any of which makes the projection too noisy
    to trust.
    """
    if len(metrics) < 2:
        return None
    miles = float(metrics[-1]["value_numeric"] - metrics[0]["value_numeric"])
    days = (metrics[-1]["recorded_at"] - metrics[0]["recorded_at"]).days
    if days < 7 or miles <= 0:
        return None
    return miles / days


def _predicted_due_iso(
    current_mileage: Optional[int],
    next_due_mileage: Optional[int],
    mpd: Optional[float],
    today: date,
) -> Optional[str]:
    """ISO date when ``current_mileage`` is projected to reach ``next_due_mileage``."""
    if not isinstance(next_due_mileage, int) or not isinstance(current_mileage, int):
        return None
    if not mpd:
        return None
    miles_to_go = next_due_mileage - current_mileage
    days_to_go = int(miles_to_go / mpd)
    return (today + timedelta(days=days_to_go)).isoformat()


def _add_months(d: date, months: int) -> date:
    """Add `months` calendar months to `d`, clamping the day to the target
    month's length so Jan 31 + 1 month → Feb 28/29, not an error."""
    year = d.year + (d.month - 1 + months) // 12
    month = (d.month - 1 + months) % 12 + 1
    # Days in the target month — Feb is special, the rest are well-known.
    if month == 2:
        last = 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28
    elif month in (4, 6, 9, 11):
        last = 30
    else:
        last = 31
    return date(year, month, min(d.day, last))


async def _recompute_schedules(conn, vehicle_id: uuid.UUID, current_mileage: int, update_date: date):
    """Shared logic used by mileage-log and template-apply.

    Updates next_due_mileage, computes predicted_due_date from recent mpd,
    and creates action items for anything due within 500 mi or already past.
    """
    schedules = await conn.fetch(
        """SELECT id, data, source_document_id, subject_id
           FROM structured_records
           WHERE deleted_at IS NULL
             AND record_type = 'maintenance_schedule'
             AND data->>'vehicle_record_id' = $1""",
        str(vehicle_id),
    )

    # Recent miles-per-day cadence from time_series_metrics. Subject-scoped
    # (not vehicle-scoped) — see comment in merge endpoint about commingled
    # mileage when one subject owns multiple vehicles.
    mpd: Optional[float] = None
    if schedules and schedules[0]["subject_id"]:
        metrics = await conn.fetch(
            """SELECT value_numeric, recorded_at FROM time_series_metrics
               WHERE subject_id = $1 AND metric_type = 'mileage'
                 AND recorded_at >= NOW() - INTERVAL '90 days'
               ORDER BY recorded_at""",
            schedules[0]["subject_id"],
        )
        mpd = _mpd_from_metrics(metrics)

    actions_created: list[str] = []
    for s in schedules:
        sdata = s["data"] if isinstance(s["data"], dict) else json.loads(s["data"])
        interval_miles = sdata.get("interval_miles")
        last_mileage = sdata.get("last_service_mileage")

        recomputed = False
        if isinstance(interval_miles, int) and isinstance(last_mileage, int):
            new_next_mileage = last_mileage + interval_miles
            if sdata.get("next_due_mileage") != new_next_mileage:
                sdata["next_due_mileage"] = new_next_mileage
                recomputed = True

        # Date-based next_due. last_service_date + interval_months is the
        # source of truth for the timeline's solid markers; without this the
        # schedule keeps next_due_date null forever and only projection
        # markers ever render.
        interval_months = sdata.get("interval_months")
        last_service_date_raw = sdata.get("last_service_date")
        if isinstance(interval_months, int) and last_service_date_raw:
            try:
                lsd = (last_service_date_raw if isinstance(last_service_date_raw, date)
                       else date.fromisoformat(last_service_date_raw))
                new_next_date = _add_months(lsd, interval_months).isoformat()
                if sdata.get("next_due_date") != new_next_date:
                    sdata["next_due_date"] = new_next_date
                    recomputed = True
            except (TypeError, ValueError):
                pass

        # Predicted ETA: when will current_mileage hit next_due_mileage?
        ndm = sdata.get("next_due_mileage")
        new_predicted = _predicted_due_iso(current_mileage, ndm, mpd, update_date)
        if sdata.get("predicted_due_date") != new_predicted:
            sdata["predicted_due_date"] = new_predicted
            recomputed = True

        if recomputed:
            await conn.execute(
                "UPDATE structured_records SET data = $1::jsonb WHERE id = $2",
                sdata, s["id"],
            )

        # Mileage-based action: due within 500 miles or past due.
        if isinstance(ndm, int):
            miles_until = ndm - current_mileage
            if miles_until <= _MILEAGE_DUE_WINDOW:
                title = f"Vehicle: {sdata.get('service_type') or 'Maintenance'}"
                if miles_until < 0:
                    desc = f"Overdue by {abs(miles_until)} miles (current {current_mileage:,}, due {ndm:,})"
                    priority = "high"
                else:
                    desc = f"Due in {miles_until} miles (current {current_mileage:,}, due {ndm:,})"
                    priority = "medium"

                existing = await conn.fetchval(
                    """SELECT 1 FROM action_items
                       WHERE source_record_id = $1 AND status = 'pending'
                         AND deleted_at IS NULL LIMIT 1""",
                    s["id"],
                )
                if not existing:
                    new_id = await conn.fetchval(
                        """INSERT INTO action_items
                               (domain, subject_id, title, description, due_date,
                                source_type, source_record_id, priority, recurrence_rule)
                           VALUES ('auto', $1, $2, $3, $4, 'recurring', $5, $6, 'interval')
                           RETURNING id""",
                        s["subject_id"], title, desc, update_date, s["id"], priority,
                    )
                    actions_created.append(str(new_id))

        # Date-based action item.
        if sdata.get("next_due_date"):
            try:
                await ensure_recurring_action_item(
                    conn,
                    record_type="maintenance_schedule",
                    record_id=s["id"],
                    data=sdata,
                    subject_id=s["subject_id"],
                    source_document_id=s["source_document_id"],
                )
            except Exception as e:
                logger.warning(f"Maintenance action item failed for {s['id']}: {e}")

    return actions_created


# Schedule CRUD endpoints live in routers/maintenance.py (mounted under
# /api/maintenance-schedules) — they reuse _fetch_vehicle/_fetch_schedule/
# _recompute_schedules from this module.
