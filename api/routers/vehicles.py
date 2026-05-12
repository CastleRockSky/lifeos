"""
routers/vehicles.py — Vehicle-specific endpoints (Phase 7).

Vehicles themselves are structured_records (record_type='vehicle'); CRUD goes
through /api/records. This router holds the vehicle-specific operations:
  - Mileage logging
  - Maintenance recompute
"""

import json
import logging
import uuid
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from database import get_pool
from helpers import audit_log, get_user_email
from recurrences import ensure_recurring_action_item

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vehicles", tags=["vehicles"])

# Within this many miles of next_due_mileage we'll create an action item.
_MILEAGE_DUE_WINDOW = 500


class MileageUpdate(BaseModel):
    mileage: int
    date: Optional[date] = None
    notes: Optional[str] = None


def _data(row) -> dict:
    return row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])


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
            json.dumps(vdata, default=str), vid,
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

        # Recompute maintenance schedules for this vehicle.
        schedules = await conn.fetch("""
            SELECT id, data, source_document_id
            FROM structured_records
            WHERE deleted_at IS NULL
              AND record_type = 'maintenance_schedule'
              AND data->>'vehicle_record_id' = $1
        """, str(vid))

        actions_created: list[str] = []
        actions_updated_records: list[str] = []

        for s in schedules:
            sdata = _data(s)
            interval_miles = sdata.get("interval_miles")
            last_mileage = sdata.get("last_service_mileage")

            recomputed = False
            if isinstance(interval_miles, int) and isinstance(last_mileage, int):
                new_next_mileage = last_mileage + interval_miles
                if sdata.get("next_due_mileage") != new_next_mileage:
                    sdata["next_due_mileage"] = new_next_mileage
                    recomputed = True

            if recomputed:
                await conn.execute(
                    "UPDATE structured_records SET data = $1::jsonb WHERE id = $2",
                    json.dumps(sdata, default=str), s["id"],
                )
                actions_updated_records.append(str(s["id"]))

            # Mileage-based action: due within 500 miles or past due.
            ndm = sdata.get("next_due_mileage")
            if isinstance(ndm, int):
                miles_until = ndm - body.mileage
                if miles_until <= _MILEAGE_DUE_WINDOW:
                    title = (
                        f"Vehicle: {sdata.get('service_type') or 'Maintenance'}"
                    )
                    if miles_until < 0:
                        desc = f"Overdue by {abs(miles_until)} miles (current {body.mileage:,}, due {ndm:,})"
                        priority = "high"
                    else:
                        desc = f"Due in {miles_until} miles (current {body.mileage:,}, due {ndm:,})"
                        priority = "medium"

                    existing = await conn.fetchval("""
                        SELECT 1 FROM action_items
                        WHERE source_record_id = $1
                          AND status = 'pending'
                          AND deleted_at IS NULL
                        LIMIT 1
                    """, s["id"])
                    if not existing:
                        new_id = await conn.fetchval("""
                            INSERT INTO action_items
                                (domain, subject_id, title, description, due_date,
                                 source_type, source_record_id, priority, recurrence_rule)
                            VALUES ('auto', $1, $2, $3, $4, 'recurring', $5, $6, 'interval')
                            RETURNING id
                        """,
                            vehicle["subject_id"],
                            title,
                            desc,
                            update_date,
                            s["id"],
                            priority,
                        )
                        actions_created.append(str(new_id))

            # Date-based action item (covers schedules with next_due_date set).
            if sdata.get("next_due_date"):
                try:
                    await ensure_recurring_action_item(
                        conn,
                        record_type="maintenance_schedule",
                        record_id=s["id"],
                        data=sdata,
                        subject_id=vehicle["subject_id"],
                        source_document_id=s["source_document_id"],
                    )
                except Exception as e:
                    logger.warning(f"Maintenance action item failed for {s['id']}: {e}")

    await audit_log("update", get_user_email(request), "structured_records",
                    vehicle_id, {"mileage": body.mileage})

    return {
        "data": {
            "vehicle_id": vehicle_id,
            "mileage": body.mileage,
            "mileage_updated": update_date.isoformat(),
            "schedules_recomputed": actions_updated_records,
            "actions_created": actions_created,
        }
    }
