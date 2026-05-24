"""
routers/maintenance.py — Maintenance schedule CRUD + template library
(Auto-redesign Phase 3).

`maintenance_schedule` records live in structured_records. These endpoints
are thin wrappers around that table with vehicle-aware bookkeeping (auto
recompute, action-item generation, etc.).

`apply-template` is per-vehicle and lives on routers/vehicles.py.
"""

import logging
import uuid
from datetime import date
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from database import get_pool
from helpers import audit_log, get_user_email
from schemas import validate_record
from data.maintenance_templates import list_templates_summary, get_template
# Reuse helpers + recompute logic that already live in the vehicles router.
from routers.vehicles import (
    _fetch_vehicle, _fetch_schedule, _data, _recompute_schedules,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["maintenance"])


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


@router.get("/maintenance-templates")
async def list_maintenance_templates():
    """Summary list of templates for the picker UI."""
    return {"data": list_templates_summary()}


@router.get("/maintenance-templates/{template_key}")
async def get_maintenance_template(template_key: str):
    """Full template detail including every schedule entry."""
    tpl = get_template(template_key)
    if not tpl:
        raise HTTPException(404, f"Template not found: {template_key}")
    return {"data": {"key": template_key, **tpl}}


@router.post("/maintenance-schedules")
async def create_schedule(body: ScheduleCreate, request: Request):
    if body.interval_miles is None and body.interval_months is None:
        raise HTTPException(400, "At least one of interval_miles or interval_months is required")

    try:
        vid = uuid.UUID(body.vehicle_record_id)
    except ValueError:
        raise HTTPException(400, "Invalid vehicle_record_id")

    payload = body.model_dump(exclude_none=True)
    cleaned = validate_record("maintenance_schedule", payload)

    pool = get_pool()
    async with pool.acquire() as conn:
        vehicle = await _fetch_vehicle(conn, vid)
        row = await conn.fetchrow(
            """INSERT INTO structured_records
                   (record_type, domain, subject_id, data)
               VALUES ('maintenance_schedule', 'auto', $1, $2)
               RETURNING *""",
            vehicle["subject_id"], cleaned,
        )
        vdata = _data(vehicle)
        if vdata.get("current_mileage") is not None:
            await _recompute_schedules(conn, vid, vdata["current_mileage"], date.today())

    await audit_log("create", get_user_email(request), "structured_records",
                    str(row["id"]), {"record_type": "maintenance_schedule"})
    return {"data": {"id": str(row["id"]), "data": cleaned}}


@router.patch("/maintenance-schedules/{schedule_id}")
async def update_schedule(schedule_id: str, body: ScheduleUpdate, request: Request):
    try:
        sid = uuid.UUID(schedule_id)
    except ValueError:
        raise HTTPException(400, "Invalid schedule id")

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(400, "No fields to update")

    pool = get_pool()
    async with pool.acquire() as conn:
        sched = await _fetch_schedule(conn, sid)
        merged = dict(_data(sched))
        merged.update(updates)
        if merged.get("interval_miles") is None and merged.get("interval_months") is None:
            raise HTTPException(400, "At least one of interval_miles or interval_months is required")
        cleaned = validate_record("maintenance_schedule", merged)
        await conn.execute(
            "UPDATE structured_records SET data = $1::jsonb WHERE id = $2",
            cleaned, sid,
        )
        vid_str = cleaned.get("vehicle_record_id")
        if vid_str and any(k in updates for k in (
            "interval_miles", "interval_months", "last_service_date", "last_service_mileage"
        )):
            try:
                vid = uuid.UUID(vid_str)
                vehicle = await _fetch_vehicle(conn, vid)
                vdata = _data(vehicle)
                if vdata.get("current_mileage") is not None:
                    await _recompute_schedules(conn, vid, vdata["current_mileage"], date.today())
            except (ValueError, HTTPException):
                pass

    await audit_log("update", get_user_email(request), "structured_records",
                    schedule_id, {"fields": list(updates.keys())})
    return {"data": {"id": schedule_id, "data": cleaned}}


@router.delete("/maintenance-schedules/{schedule_id}")
async def delete_schedule(schedule_id: str, request: Request):
    """Hard-delete a schedule. Pending action items it produced get tagged
    with metadata.schedule_deleted = true so the UI can distinguish them
    from active recurring items."""
    try:
        sid = uuid.UUID(schedule_id)
    except ValueError:
        raise HTTPException(400, "Invalid schedule id")

    pool = get_pool()
    async with pool.acquire() as conn:
        await _fetch_schedule(conn, sid)
        await conn.execute(
            """UPDATE action_items
               SET metadata = COALESCE(metadata, '{}'::jsonb)
                            || jsonb_build_object('schedule_deleted', true)
               WHERE source_record_id = $1 AND deleted_at IS NULL""",
            sid,
        )
        await conn.execute("DELETE FROM structured_records WHERE id = $1", sid)

    await audit_log("delete", get_user_email(request), "structured_records",
                    schedule_id, {"record_type": "maintenance_schedule"})
    return {"data": {"id": schedule_id, "deleted": True}}
