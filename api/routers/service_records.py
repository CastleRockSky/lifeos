"""
routers/service_records.py — Service record CRUD (Auto-redesign Phase 4).

service_record rows live in structured_records. These endpoints are thin
wrappers around that table with the Phase 4 affordances:
  - Optional link_to_schedule_id closes the loop with Phase 3: logging
    "I had this oil change" updates the schedule's last_service_* and
    re-runs the recompute so next_due_* moves forward.
  - DELETE is a hard delete (these can be re-entered easily and we don't
    want soft-deleted services skewing cost rollups).

Cost summary lives on routers/vehicles.py:cost_summary.
"""

import logging
import uuid
from datetime import date
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from database import get_pool
from helpers import audit_log, get_user_email
from schemas import validate_record
from routers.vehicles import (
    _fetch_vehicle, _fetch_schedule, _data, _recompute_schedules,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["service-records"])


class ServiceRecordCreate(BaseModel):
    vehicle_record_id: str
    date: date
    service_type: str
    mileage: Optional[int] = None
    category: Optional[str] = "preventive"
    provider: Optional[str] = None
    cost: Optional[float] = None
    parts: list[str] = []
    notes: Optional[str] = None
    document_id: Optional[str] = None


class ServiceRecordUpdate(BaseModel):
    date: Optional[date] = None
    mileage: Optional[int] = None
    service_type: Optional[str] = None
    category: Optional[str] = None
    provider: Optional[str] = None
    cost: Optional[float] = None
    parts: Optional[list[str]] = None
    notes: Optional[str] = None
    document_id: Optional[str] = None


async def _fetch_service(conn, sid: uuid.UUID):
    row = await conn.fetchrow(
        """SELECT id, record_type, subject_id, data
           FROM structured_records
           WHERE id = $1 AND deleted_at IS NULL""",
        sid,
    )
    if not row or row["record_type"] != "service_record":
        raise HTTPException(404, "Service record not found")
    return row


@router.post("/service-records")
async def create_service_record(
    body: ServiceRecordCreate, request: Request,
    link_to_schedule_id: Optional[str] = Query(None),
):
    """Create a service record. If link_to_schedule_id is provided, also
    update that schedule's last_service_* fields to match and re-run the
    recompute so next_due_* moves forward."""
    try:
        vid = uuid.UUID(body.vehicle_record_id)
    except ValueError:
        raise HTTPException(400, "Invalid vehicle_record_id")

    sched_uuid: Optional[uuid.UUID] = None
    if link_to_schedule_id:
        try:
            sched_uuid = uuid.UUID(link_to_schedule_id)
        except ValueError:
            raise HTTPException(400, "Invalid link_to_schedule_id")

    payload = body.model_dump(exclude_none=True)
    cleaned = validate_record("service_record", payload)

    pool = get_pool()
    async with pool.acquire() as conn:
        vehicle = await _fetch_vehicle(conn, vid)
        row = await conn.fetchrow(
            """INSERT INTO structured_records
                   (record_type, domain, subject_id, data, source_document_id)
               VALUES ('service_record', 'auto', $1, $2, $3)
               RETURNING *""",
            vehicle["subject_id"], cleaned,
            uuid.UUID(body.document_id) if body.document_id else None,
        )

        # Schedule linking: bump last_service_date / last_service_mileage on
        # the target schedule, then recompute so next_due_* moves forward.
        if sched_uuid:
            sched = await _fetch_schedule(conn, sched_uuid)
            sdata = dict(_data(sched))
            sdata["last_service_date"] = body.date.isoformat()
            if body.mileage is not None:
                sdata["last_service_mileage"] = body.mileage
            # Validate the merged blob (catches typos that snuck in).
            sdata = validate_record("maintenance_schedule", sdata)
            await conn.execute(
                "UPDATE structured_records SET data = $1::jsonb WHERE id = $2",
                sdata, sched_uuid,
            )
            # The service that was due is now done — close out any pending
            # action item the schedule produced. Without this they'd linger
            # as overdue even after the user logged the completed service.
            await conn.execute(
                """UPDATE action_items
                   SET status = 'completed', completed_at = NOW()
                   WHERE source_record_id = $1 AND status = 'pending'
                     AND deleted_at IS NULL""",
                sched_uuid,
            )
            vdata = _data(vehicle)
            cm = vdata.get("current_mileage")
            if cm is not None:
                await _recompute_schedules(conn, vid, cm, date.today())

    await audit_log(
        "create", get_user_email(request), "structured_records",
        str(row["id"]),
        {"record_type": "service_record", "linked_schedule": link_to_schedule_id},
    )
    return {"data": {"id": str(row["id"]), "data": cleaned,
                     "linked_schedule_id": link_to_schedule_id}}


@router.patch("/service-records/{service_id}")
async def update_service_record(service_id: str, body: ServiceRecordUpdate, request: Request):
    try:
        sid = uuid.UUID(service_id)
    except ValueError:
        raise HTTPException(400, "Invalid service id")

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(400, "No fields to update")

    pool = get_pool()
    async with pool.acquire() as conn:
        rec = await _fetch_service(conn, sid)
        merged = dict(_data(rec))
        merged.update(updates)
        cleaned = validate_record("service_record", merged)
        await conn.execute(
            "UPDATE structured_records SET data = $1::jsonb WHERE id = $2",
            cleaned, sid,
        )

    await audit_log(
        "update", get_user_email(request), "structured_records",
        service_id, {"fields": list(updates.keys())},
    )
    return {"data": {"id": service_id, "data": cleaned}}


@router.delete("/service-records/{service_id}")
async def delete_service_record(service_id: str, request: Request):
    """Hard delete — cost rollups should not double-count soft-deleted entries."""
    try:
        sid = uuid.UUID(service_id)
    except ValueError:
        raise HTTPException(400, "Invalid service id")

    pool = get_pool()
    async with pool.acquire() as conn:
        await _fetch_service(conn, sid)
        await conn.execute("DELETE FROM structured_records WHERE id = $1", sid)

    await audit_log(
        "delete", get_user_email(request), "structured_records",
        service_id, {"record_type": "service_record"},
    )
    return {"data": {"id": service_id, "deleted": True}}
