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
    document_count = await conn.fetchval(
        "SELECT COUNT(*) FROM documents WHERE deleted_at IS NULL AND linked_record_id = $1",
        uuid.UUID(vehicle_id),
    )
    return {
        "maintenance_schedules": schedule_count or 0,
        "service_records": service_count or 0,
        "action_items": action_count or 0,
        "documents": document_count or 0,
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
            # Phase 6: documents linked to the source follow the merge.
            docs_moved = await conn.execute(
                """UPDATE documents SET linked_record_id = $2
                   WHERE linked_record_id = $1 AND deleted_at IS NULL""",
                src_id, tgt_id,
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
            "documents_moved": _parse_update_count(docs_moved),
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


# ── Cost summary (Auto-redesign Phase 4) ────────────────────────────────

@router.get("/{vehicle_id}/cost-summary")
async def cost_summary(vehicle_id: str):
    """Per-vehicle service spend rollups: lifetime, $/mile, YTD, by category,
    by year. The math lives in _compute_cost_summary so it's unit-testable;
    this endpoint just gathers inputs from the DB."""
    try:
        vid = uuid.UUID(vehicle_id)
    except ValueError:
        raise HTTPException(400, "Invalid vehicle id")

    pool = get_pool()
    async with pool.acquire() as conn:
        vehicle = await _fetch_vehicle(conn, vid)
        vdata = _data(vehicle)
        current_mileage = vdata.get("current_mileage")

        svc_rows = await conn.fetch(
            """SELECT data FROM structured_records
               WHERE record_type = 'service_record'
                 AND deleted_at IS NULL
                 AND data->>'vehicle_record_id' = $1""",
            str(vid),
        )
        services = [
            r["data"] if isinstance(r["data"], dict) else json.loads(r["data"])
            for r in svc_rows
        ]

        # Mileage observations from the time-series table — only for this
        # vehicle's subject. Combined with each service's mileage to find
        # the earliest known reading.
        mileage_history: list[int] = []
        if vehicle["subject_id"]:
            rows = await conn.fetch(
                """SELECT value_numeric FROM time_series_metrics
                   WHERE subject_id = $1 AND metric_type = 'mileage'""",
                vehicle["subject_id"],
            )
            mileage_history = [int(r["value_numeric"]) for r in rows
                               if r["value_numeric"] is not None]

    return {"data": _compute_cost_summary(
        services, current_mileage, mileage_history, date.today(),
    )}


# ── Mileage trends (Auto-redesign Phase 8) ──────────────────────────────

# Below these thresholds the chart still renders but ETAs / per-month
# stats stop displaying — the projection would be too noisy to trust.
_MIN_POINTS_FOR_TRENDS = 3
_MIN_DAYS_FOR_TRENDS = 7


def _bucket_mileage_points(metrics, granularity: str) -> list[dict]:
    """Reduce raw mileage metrics to one point per bucket.

    Last reading within each bucket wins (a chart at week granularity
    should reflect the odometer at week-end, not its midweek dip into a
    re-entry typo). Metrics must be chronologically ordered.
    """
    if not metrics:
        return []

    def _key(d: date) -> str:
        if granularity == "day":
            return d.isoformat()
        if granularity == "week":
            # ISO week-year-week so weeks straddling year boundaries don't collide.
            year, week, _ = d.isocalendar()
            return f"{year}-W{week:02d}"
        # default month
        return f"{d.year}-{d.month:02d}"

    buckets: dict[str, dict] = {}
    for m in metrics:
        recorded = m["recorded_at"]
        d = recorded.date() if hasattr(recorded, "date") else recorded
        key = _key(d)
        # Last write wins because metrics arrive in order.
        buckets[key] = {"date": d.isoformat(), "value": float(m["value_numeric"])}

    return [buckets[k] for k in sorted(buckets)]


def _assess_data_quality(metrics) -> str:
    """Classify how trustworthy the cadence projection is.

    - ``insufficient``: fewer than _MIN_POINTS_FOR_TRENDS readings OR
      span <_MIN_DAYS_FOR_TRENDS days. Frontend should hide ETAs.
    - ``limited``: enough points to draw a chart but the span is between
      7 and 30 days — chart shows but "per month" extrapolations are
      marked rough.
    - ``good``: ≥30 days of history.
    """
    if len(metrics) < _MIN_POINTS_FOR_TRENDS:
        return "insufficient"
    first = metrics[0]["recorded_at"]
    last = metrics[-1]["recorded_at"]
    span_days = (last - first).days
    if span_days < _MIN_DAYS_FOR_TRENDS:
        return "insufficient"
    if span_days < 30:
        return "limited"
    return "good"


@router.get("/{vehicle_id}/mileage-history")
async def mileage_history(
    vehicle_id: str,
    since: Optional[str] = None,
    granularity: str = "month",
):
    """Mileage time-series for the vehicle's subject. Returns bucketed
    points + recent-cadence stats + a data_quality classification."""
    try:
        vid = uuid.UUID(vehicle_id)
    except ValueError:
        raise HTTPException(400, "Invalid vehicle id")
    if granularity not in ("day", "week", "month"):
        raise HTTPException(400, "granularity must be one of: day, week, month")

    since_date: Optional[date] = None
    if since:
        try:
            since_date = date.fromisoformat(since)
        except ValueError:
            raise HTTPException(400, f"Invalid since date: {since}")

    pool = get_pool()
    async with pool.acquire() as conn:
        vehicle = await _fetch_vehicle(conn, vid)
        if not vehicle["subject_id"]:
            # No subject means no metrics were ever written; mileage was only
            # ever stored on the vehicle's current_mileage field.
            return {"data": {
                "points": [], "miles_per_day_recent": None,
                "miles_per_month_recent": None, "data_quality": "insufficient",
            }}

        params = [vehicle["subject_id"]]
        where_extra = ""
        if since_date:
            params.append(since_date)
            where_extra = " AND recorded_at >= $2"
        rows = await conn.fetch(
            f"""SELECT value_numeric, recorded_at FROM time_series_metrics
                WHERE subject_id = $1 AND metric_type = 'mileage'{where_extra}
                ORDER BY recorded_at""",
            *params,
        )

    points = _bucket_mileage_points(rows, granularity)
    mpd = _mpd_from_metrics(rows)

    return {"data": {
        "points": points,
        "miles_per_day_recent": round(mpd, 2) if mpd is not None else None,
        "miles_per_month_recent": round(mpd * 30, 1) if mpd is not None else None,
        "data_quality": _assess_data_quality(rows),
        "first_log_date": points[0]["date"] if points else None,
        "last_log_date": points[-1]["date"] if points else None,
        "total_points": len(rows),
    }}


# ── Documents panel (Auto-redesign Phase 6) ─────────────────────────────

@router.get("/{vehicle_id}/documents")
async def vehicle_documents(vehicle_id: str):
    """Documents linked to this vehicle, grouped by category, newest first
    within each group. The UI uses this to render the per-vehicle Documents
    section without having to fetch the whole global Documents list."""
    try:
        vid = uuid.UUID(vehicle_id)
    except ValueError:
        raise HTTPException(400, "Invalid vehicle id")

    pool = get_pool()
    async with pool.acquire() as conn:
        await _fetch_vehicle(conn, vid)
        rows = await conn.fetch(
            """SELECT id, title, category, file_type, mime_type,
                      document_date, expiration_date, expiration_acknowledged_at,
                      ai_status, review_status, created_at, ingested_at
               FROM documents
               WHERE linked_record_id = $1 AND deleted_at IS NULL
               ORDER BY COALESCE(document_date, created_at::date) DESC,
                        created_at DESC""",
            vid,
        )

    grouped: dict[str, list[dict]] = {}
    for r in rows:
        cat = r["category"] or "uncategorized"
        grouped.setdefault(cat, []).append({
            "id": str(r["id"]),
            "title": r["title"],
            "category": cat,
            "file_type": r["file_type"],
            "mime_type": r["mime_type"],
            "document_date": r["document_date"].isoformat() if r["document_date"] else None,
            "expiration_date": r["expiration_date"].isoformat() if r["expiration_date"] else None,
            "expiration_acknowledged": r["expiration_acknowledged_at"] is not None,
            "ai_status": r["ai_status"],
            "review_status": r["review_status"],
            "thumbnail_url": f"/api/documents/{r['id']}/thumbnail",
            "file_url": f"/api/documents/{r['id']}/file",
            "created_at": r["created_at"].isoformat(),
        })

    return {"data": grouped, "meta": {"total": len(rows)}}


# ── Fleet summary (Auto-redesign Phase 5) ───────────────────────────────

# Vehicle status values that should NOT count toward fleet totals. `merged`
# rows are audit-only; archived/sold/totaled vehicles still live in the DB
# but aren't part of the working fleet.
_INACTIVE_STATUSES = {"merged", "archived", "sold", "totaled"}


def _compute_fleet_summary(
    vehicles: list[dict],
    services: list[dict],
    pending_action_due_dates: list[Optional[date]],
    today: date,
    open_recalls: int = 0,
) -> dict:
    """Pure aggregation across the working fleet.

    vehicles: list of vehicle data blobs (status field is consulted; defaults
              to 'active' when absent). Inactive statuses are excluded.
    services: list of service-record data blobs across all vehicles.
              cost/date fields used; vehicle attribution doesn't matter here.
    pending_action_due_dates: due_date for each pending auto action (or None).
                              Used to count overdue items.
    today: anchor for YTD and the 60-day registration window.
    open_recalls: precomputed count of vehicle_recall rows with status='open'
                  across active vehicles (queried separately because it lives
                  in structured_records, not the input dicts here).
    """
    active = [v for v in vehicles if (v.get("status") or "active") not in _INACTIVE_STATUSES]

    total_mileage = 0
    expiring_60d = 0
    for v in active:
        m = v.get("current_mileage")
        if isinstance(m, int):
            total_mileage += m
        raw = v.get("registration_expiration")
        if raw:
            try:
                d = raw if isinstance(raw, date) else date.fromisoformat(raw)
            except (TypeError, ValueError):
                d = None
            if d is not None:
                days_until = (d - today).days
                if 0 <= days_until <= 60:
                    expiring_60d += 1

    ytd_spend = 0.0
    for s in services:
        cost = s.get("cost")
        if not isinstance(cost, (int, float)):
            continue
        raw = s.get("date")
        try:
            d = raw if isinstance(raw, date) else date.fromisoformat(raw) if isinstance(raw, str) else None
        except ValueError:
            d = None
        if d is not None and d.year == today.year:
            ytd_spend += float(cost)

    overdue = sum(
        1 for due in pending_action_due_dates
        if due is not None and due < today
    )

    return {
        "vehicle_count": len(active),
        "total_fleet_mileage": total_mileage,
        "ytd_spend": round(ytd_spend, 2),
        "open_recalls": open_recalls,
        "registrations_expiring_60d": expiring_60d,
        "overdue_maintenance": overdue,
    }


@router.get("/fleet-summary", include_in_schema=True)
async def fleet_summary():
    """Aggregate counters for the fleet header. Single endpoint so the UI
    doesn't fan out to records / actions / cost-summary per vehicle on
    every page render."""
    pool = get_pool()
    async with pool.acquire() as conn:
        veh_rows = await conn.fetch(
            """SELECT data FROM structured_records
               WHERE record_type = 'vehicle' AND deleted_at IS NULL"""
        )
        svc_rows = await conn.fetch(
            """SELECT data FROM structured_records
               WHERE record_type = 'service_record' AND deleted_at IS NULL"""
        )
        action_rows = await conn.fetch(
            """SELECT due_date FROM action_items
               WHERE domain = 'auto' AND status = 'pending' AND deleted_at IS NULL"""
        )
        # Open recalls only count if their parent vehicle is still active.
        open_recalls_count = await conn.fetchval(
            """SELECT COUNT(*) FROM structured_records r
               WHERE r.record_type = 'vehicle_recall'
                 AND r.deleted_at IS NULL
                 AND r.data->>'status' = 'open'
                 AND EXISTS (
                     SELECT 1 FROM structured_records v
                     WHERE v.id::text = r.data->>'vehicle_record_id'
                       AND v.record_type = 'vehicle'
                       AND v.deleted_at IS NULL
                       AND (v.data->>'status' IS NULL
                            OR v.data->>'status' NOT IN
                               ('merged','archived','sold','totaled')))"""
        ) or 0

    vehicles = [r["data"] if isinstance(r["data"], dict) else json.loads(r["data"])
                for r in veh_rows]
    services = [r["data"] if isinstance(r["data"], dict) else json.loads(r["data"])
                for r in svc_rows]
    due_dates = [r["due_date"] for r in action_rows]

    return {"data": _compute_fleet_summary(
        vehicles, services, due_dates, date.today(),
        open_recalls=open_recalls_count,
    )}


# ── Cross-system briefing (Auto-redesign Phase 10) ──────────────────────


def _vehicle_display_name(vdata: dict) -> str:
    """One-line label for a vehicle ("2022 Toyota Sienna"); used in
    briefing items and action item titles. Falls back to make+model only
    when the year is missing, or to "Vehicle" as a last resort."""
    parts = [vdata.get("year"), vdata.get("make"), vdata.get("model")]
    label = " ".join(str(p) for p in parts if p)
    return label or "Vehicle"


def _compute_briefing(
    vehicles: list[dict],  # list of {id, data} dicts (active only)
    overdue_actions: list[dict],  # action_items with auto domain & past due
    open_recalls: list[dict],  # vehicle_recall blobs with status='open'
    today: date,
    reg_window_days: int = 30,
) -> dict:
    """Pure aggregator for the briefing endpoint. Returns the structured
    urgent items + a one-line summary suitable for Coach to drop into a
    morning briefing.

    Items types:
      - registration: ``registration_expiration`` ≤ reg_window_days
      - overdue_maintenance: count of overdue auto action items per vehicle
      - recall: each open vehicle_recall row
    """
    items: list[dict] = []

    # Vehicle index for quick lookup when mapping actions/recalls back to
    # display names. Keyed by string id.
    vehicles_by_id: dict[str, dict] = {v["id"]: v for v in vehicles}

    # Registration expirations.
    for v in vehicles:
        data = v.get("data") or {}
        raw = data.get("registration_expiration")
        if not raw:
            continue
        try:
            d = raw if isinstance(raw, date) else date.fromisoformat(raw)
        except (TypeError, ValueError):
            continue
        days_until = (d - today).days
        if days_until > reg_window_days:
            continue
        name = _vehicle_display_name(data)
        if days_until < 0:
            text = f"{name} registration expired {abs(days_until)} day{'s' if abs(days_until) != 1 else ''} ago"
        elif days_until == 0:
            text = f"{name} registration expires today"
        else:
            text = f"{name} registration expires in {days_until} day{'s' if days_until != 1 else ''}"
        items.append({
            "vehicle_id": v["id"], "vehicle_name": name,
            "kind": "registration", "text": text,
            "severity": "high" if days_until <= 7 else "warning",
            "days_until": days_until,
        })

    # Overdue maintenance — aggregate per vehicle by metadata.vehicle_id.
    overdue_by_vehicle: dict[str, list[dict]] = {}
    for a in overdue_actions:
        vid = (a.get("metadata") or {}).get("vehicle_id")
        if vid:
            overdue_by_vehicle.setdefault(vid, []).append(a)
    for vid, actions in overdue_by_vehicle.items():
        v = vehicles_by_id.get(vid)
        if not v:
            continue
        name = _vehicle_display_name(v.get("data") or {})
        n = len(actions)
        text = (f"{name} {n} maintenance item{'s' if n != 1 else ''} overdue")
        items.append({
            "vehicle_id": vid, "vehicle_name": name,
            "kind": "overdue_maintenance", "text": text,
            "severity": "high", "count": n,
        })

    # Open recalls — one item per recall (severity high).
    for r in open_recalls:
        vid = r.get("vehicle_record_id")
        v = vehicles_by_id.get(vid) if vid else None
        name = _vehicle_display_name(v["data"]) if v else "Vehicle"
        component = (r.get("component") or "open recall").strip()
        text = f"{name} open recall: {component}"
        items.append({
            "vehicle_id": vid, "vehicle_name": name,
            "kind": "recall", "text": text, "severity": "high",
            "campaign": r.get("nhtsa_campaign_number"),
        })

    # Sort: high severity first, then by days_until (None → end).
    items.sort(key=lambda x: (
        0 if x["severity"] == "high" else 1,
        x.get("days_until") if x.get("days_until") is not None else 99999,
    ))

    summary_line = None
    if items:
        # "Auto: X. Y. Z." — concise enough for a morning briefing line.
        summary_line = "Auto: " + ". ".join(it["text"] for it in items[:4]) + "."

    return {
        "as_of": today.isoformat(),
        "has_urgent": bool(items),
        "items": items,
        "summary_line": summary_line,
    }


@router.get("/briefing", include_in_schema=True)
async def auto_briefing():
    """Auto-domain urgent items in a Coach-consumable structure. Returns
    registration expirations within 30 days, overdue maintenance, and
    open recalls. Designed for agent consumption — the `summary_line`
    drops straight into a morning briefing."""
    pool = get_pool()
    async with pool.acquire() as conn:
        veh_rows = await conn.fetch(
            """SELECT id, data FROM structured_records
               WHERE record_type = 'vehicle' AND deleted_at IS NULL
                 AND (data->>'status' IS NULL
                      OR data->>'status' NOT IN
                         ('merged','archived','sold','totaled'))"""
        )
        vehicles = [
            {"id": str(r["id"]),
             "data": r["data"] if isinstance(r["data"], dict) else json.loads(r["data"])}
            for r in veh_rows
        ]
        # Overdue actions: domain='auto', status='pending', due_date < today.
        action_rows = await conn.fetch(
            """SELECT id, title, metadata, due_date, source_record_id
               FROM action_items
               WHERE domain = 'auto' AND status = 'pending'
                 AND deleted_at IS NULL
                 AND due_date IS NOT NULL AND due_date < $1""",
            date.today(),
        )
        overdue = [
            {"id": str(r["id"]), "title": r["title"], "due_date": r["due_date"].isoformat(),
             "metadata": r["metadata"] if isinstance(r["metadata"], dict)
                         else (json.loads(r["metadata"]) if r["metadata"] else {})}
            for r in action_rows
        ]
        recall_rows = await conn.fetch(
            """SELECT data FROM structured_records
               WHERE record_type = 'vehicle_recall' AND deleted_at IS NULL
                 AND data->>'status' = 'open'"""
        )
        open_recalls = [
            r["data"] if isinstance(r["data"], dict) else json.loads(r["data"])
            for r in recall_rows
        ]

    return {"data": _compute_briefing(vehicles, overdue, open_recalls, date.today())}


# ── Business mileage nudge (Auto-redesign Phase 10) ─────────────────────

# Below this many "unaccounted" miles in the window we don't bother nudging
# — likely commute / errands / personal trips that wouldn't be billable.
_NUDGE_MIN_UNACCOUNTED_MILES = 100


def _compute_mileage_nudge(
    odometer_delta_miles: float,
    logged_trip_miles: float,
    window_days: int,
) -> dict:
    """Compare odometer movement against logged business-trip miles. The
    delta is a rough heuristic — could include commute, personal trips,
    etc. — but a large unaccounted gap is worth flagging for review.

    Returns a structured response with ``suggest_logging`` true/false plus
    the math, so the UI can decide whether to show a nudge."""
    unaccounted = max(0.0, odometer_delta_miles - logged_trip_miles)
    suggest = unaccounted >= _NUDGE_MIN_UNACCOUNTED_MILES
    text: Optional[str] = None
    if suggest:
        text = (
            f"You drove ~{int(round(odometer_delta_miles))} mi in the last "
            f"{window_days} days but only logged "
            f"{int(round(logged_trip_miles))} mi as business. "
            f"Any of the remaining {int(round(unaccounted))} mi billable?"
        )
    return {
        "window_days": window_days,
        "odometer_delta_miles": round(odometer_delta_miles, 1),
        "logged_trip_miles": round(logged_trip_miles, 1),
        "unaccounted_miles": round(unaccounted, 1),
        "suggest_logging": suggest,
        "text": text,
    }


@router.get("/{vehicle_id}/mileage-nudge")
async def mileage_nudge(vehicle_id: str, days: int = 7):
    """Phase 10 cross-system signal: should the user be prompted to log
    business mileage this week? Compares the vehicle's odometer delta
    over the last ``days`` against the sum of logged BusinessTrip miles
    in the same window."""
    try:
        vid = uuid.UUID(vehicle_id)
    except ValueError:
        raise HTTPException(400, "Invalid vehicle id")
    if days < 1 or days > 90:
        raise HTTPException(400, "days must be between 1 and 90")

    pool = get_pool()
    async with pool.acquire() as conn:
        vehicle = await _fetch_vehicle(conn, vid)
        if not vehicle["subject_id"]:
            return {"data": _compute_mileage_nudge(0.0, 0.0, days)}

        # Odometer delta from time_series_metrics: latest minus oldest in window.
        metrics = await conn.fetch(
            """SELECT value_numeric FROM time_series_metrics
               WHERE subject_id = $1 AND metric_type = 'mileage'
                 AND recorded_at >= NOW() - ($2 || ' days')::interval
               ORDER BY recorded_at""",
            vehicle["subject_id"], str(days),
        )
        if len(metrics) >= 2:
            odo_delta = float(metrics[-1]["value_numeric"] - metrics[0]["value_numeric"])
        else:
            odo_delta = 0.0

        # Logged business trips for this vehicle in the window.
        trip_rows = await conn.fetch(
            """SELECT (data->>'miles')::float AS miles FROM structured_records
               WHERE record_type = 'business_trip' AND deleted_at IS NULL
                 AND data->>'vehicle_record_id' = $1
                 AND (data->>'date')::date >= $2""",
            str(vid),
            date.today() - timedelta(days=days),
        )
        logged = sum((r["miles"] or 0.0) for r in trip_rows)

    return {"data": _compute_mileage_nudge(odo_delta, logged, days)}


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


# Below this many miles of history, $/mile is too noisy to be useful — spec
# guardrail to keep absurd numbers off the page when a vehicle is brand new
# or has just one service log.
_MIN_HISTORY_MILES_FOR_PER_MILE = 1000


def _compute_cost_summary(
    services: list[dict],
    current_mileage: Optional[int],
    mileage_history: list[int],
    today: date,
) -> dict:
    """Pure aggregation over a list of service-record data blobs.

    services: list of dicts with optional ``cost`` (number), ``category`` (str),
              and ``date`` (iso str or date) keys.
    current_mileage: latest known odometer for the vehicle, or None.
    mileage_history: additional mileage observations from time_series_metrics;
                     combined with each service's mileage to find the earliest
                     known reading.
    today: anchor for YTD ("current year" relative to this date).
    """
    lifetime_total = 0.0
    ytd_total = 0.0
    by_category: dict[str, float] = {}
    by_year: dict[str, float] = {}

    mileage_observations: list[int] = list(mileage_history)
    if isinstance(current_mileage, int):
        mileage_observations.append(current_mileage)

    for svc in services:
        cost = svc.get("cost")
        if not isinstance(cost, (int, float)):
            continue
        amount = float(cost)
        lifetime_total += amount

        # Date parsing — service.date may be a date object or iso string.
        raw_date = svc.get("date")
        d: Optional[date]
        if isinstance(raw_date, date):
            d = raw_date
        elif isinstance(raw_date, str):
            try:
                d = date.fromisoformat(raw_date)
            except ValueError:
                d = None
        else:
            d = None
        if d is not None:
            if d.year == today.year:
                ytd_total += amount
            year_key = str(d.year)
            by_year[year_key] = by_year.get(year_key, 0.0) + amount

        cat = svc.get("category") or "other"
        by_category[cat] = by_category.get(cat, 0.0) + amount

        m = svc.get("mileage")
        if isinstance(m, int):
            mileage_observations.append(m)

    per_mile: Optional[float] = None
    if isinstance(current_mileage, int) and mileage_observations:
        earliest = min(mileage_observations)
        span = current_mileage - earliest
        if span >= _MIN_HISTORY_MILES_FOR_PER_MILE and lifetime_total > 0:
            per_mile = lifetime_total / span

    return {
        "lifetime_total": round(lifetime_total, 2),
        "lifetime_per_mile": round(per_mile, 4) if per_mile is not None else None,
        "ytd_total": round(ytd_total, 2),
        "by_category": {k: round(v, 2) for k, v in by_category.items()},
        "by_year": {k: round(v, 2) for k, v in by_year.items()},
    }


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

    # Resolve the vehicle's display name once so action items get titled
    # "Sienna: Oil change" instead of the generic "Vehicle: Oil change"
    # (Phase 10 cross-system integration).
    vrow = await conn.fetchrow(
        "SELECT data FROM structured_records WHERE id = $1 AND deleted_at IS NULL",
        vehicle_id,
    )
    vdata = (vrow["data"] if vrow and isinstance(vrow["data"], dict)
             else (json.loads(vrow["data"]) if vrow else {}))
    vehicle_name = " ".join(
        str(p) for p in [vdata.get("year"), vdata.get("make"), vdata.get("model")] if p
    ) or None
    auto_metadata = {"vehicle_id": str(vehicle_id)}

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
                svc = sdata.get("service_type") or "Maintenance"
                title = f"{vehicle_name}: {svc}" if vehicle_name else f"Vehicle: {svc}"
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
                                source_type, source_record_id, priority,
                                recurrence_rule, metadata)
                           VALUES ('auto', $1, $2, $3, $4, 'recurring', $5, $6,
                                   'interval', $7)
                           RETURNING id""",
                        s["subject_id"], title, desc, update_date, s["id"], priority,
                        auto_metadata,
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
                    vehicle_name=vehicle_name,
                    extra_metadata=auto_metadata,
                )
            except Exception as e:
                logger.warning(f"Maintenance action item failed for {s['id']}: {e}")

    return actions_created


# Schedule CRUD endpoints live in routers/maintenance.py (mounted under
# /api/maintenance-schedules) — they reuse _fetch_vehicle/_fetch_schedule/
# _recompute_schedules from this module.
