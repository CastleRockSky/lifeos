"""
routers/recalls.py — NHTSA vehicle-recall surfacing (Auto-redesign Phase 7).

vehicle_recall rows live in structured_records. These endpoints expose:
  - List recalls for a vehicle (optionally filtered by status).
  - Acknowledge / mark-resolved lifecycle transitions.
  - Manual refresh: re-fetch from NHTSA and ingest new campaigns.

Background scheduling (weekly cron per the spec) is NOT included here;
manual refresh covers the immediate need and an opt-in scheduled job
can be layered on later without changing the data model.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from database import get_pool
from helpers import audit_log, get_user_email
from nhtsa import (
    fetch_recalls_for_vehicle, filter_new_recalls, parse_nhtsa_results,
)
from routers.vehicles import _data, _fetch_vehicle
from schemas import validate_record


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["recalls"])


class ResolveBody(BaseModel):
    service_record_id: Optional[str] = None
    notes: Optional[str] = None


async def _fetch_recall(conn, rid: uuid.UUID):
    row = await conn.fetchrow(
        """SELECT id, record_type, subject_id, data
           FROM structured_records
           WHERE id = $1 AND deleted_at IS NULL""",
        rid,
    )
    if not row or row["record_type"] != "vehicle_recall":
        raise HTTPException(404, "Recall not found")
    return row


@router.get("/vehicles/{vehicle_id}/recalls")
async def list_recalls(vehicle_id: str, status: Optional[str] = Query(None)):
    """List recalls for a vehicle. ``status`` filters to one of
    open/acknowledged/resolved; omit to get all (open first)."""
    try:
        vid = uuid.UUID(vehicle_id)
    except ValueError:
        raise HTTPException(400, "Invalid vehicle id")

    pool = get_pool()
    async with pool.acquire() as conn:
        await _fetch_vehicle(conn, vid)
        params: list = [str(vid)]
        where = ("record_type = 'vehicle_recall' AND deleted_at IS NULL "
                 "AND data->>'vehicle_record_id' = $1")
        if status:
            params.append(status)
            where += " AND data->>'status' = $2"
        rows = await conn.fetch(
            f"SELECT id, data, created_at FROM structured_records WHERE {where} "
            # Open first, then by discovery time (newest within each bucket).
            "ORDER BY CASE data->>'status' "
            "  WHEN 'open' THEN 0 WHEN 'acknowledged' THEN 1 ELSE 2 END, "
            "created_at DESC",
            *params,
        )

    return {"data": [
        {"id": str(r["id"]),
         "data": r["data"] if isinstance(r["data"], dict) else json.loads(r["data"]),
         "created_at": r["created_at"].isoformat()}
        for r in rows
    ]}


@router.post("/recalls/{recall_id}/acknowledge")
async def acknowledge_recall(recall_id: str, request: Request):
    try:
        rid = uuid.UUID(recall_id)
    except ValueError:
        raise HTTPException(400, "Invalid recall id")

    now_iso = datetime.now(timezone.utc).isoformat()
    pool = get_pool()
    async with pool.acquire() as conn:
        recall = await _fetch_recall(conn, rid)
        data = dict(_data(recall))
        if data.get("status") == "resolved":
            raise HTTPException(400, "Recall is already resolved")
        data["status"] = "acknowledged"
        data["acknowledged_at"] = now_iso
        cleaned = validate_record("vehicle_recall", data)
        await conn.execute(
            "UPDATE structured_records SET data = $1::jsonb WHERE id = $2",
            cleaned, rid,
        )

    await audit_log("acknowledge", get_user_email(request),
                    "structured_records", recall_id,
                    {"record_type": "vehicle_recall"})
    return {"data": {"id": recall_id, "data": cleaned}}


@router.post("/recalls/{recall_id}/resolve")
async def resolve_recall(recall_id: str, body: ResolveBody, request: Request):
    try:
        rid = uuid.UUID(recall_id)
    except ValueError:
        raise HTTPException(400, "Invalid recall id")

    service_uuid: Optional[uuid.UUID] = None
    if body.service_record_id:
        try:
            service_uuid = uuid.UUID(body.service_record_id)
        except ValueError:
            raise HTTPException(400, "Invalid service_record_id")

    now_iso = datetime.now(timezone.utc).isoformat()
    pool = get_pool()
    async with pool.acquire() as conn:
        recall = await _fetch_recall(conn, rid)
        if service_uuid:
            exists = await conn.fetchval(
                """SELECT 1 FROM structured_records
                   WHERE id = $1 AND record_type = 'service_record'
                     AND deleted_at IS NULL""",
                service_uuid,
            )
            if not exists:
                raise HTTPException(400, "service_record_id not found")

        data = dict(_data(recall))
        data["status"] = "resolved"
        data["resolved_at"] = now_iso
        if service_uuid:
            data["resolved_service_record_id"] = str(service_uuid)
        if body.notes:
            data["notes"] = body.notes
        cleaned = validate_record("vehicle_recall", data)
        await conn.execute(
            "UPDATE structured_records SET data = $1::jsonb WHERE id = $2",
            cleaned, rid,
        )
        # Close out the action item we created when the recall was discovered.
        await conn.execute(
            """UPDATE action_items SET status = 'completed',
                                       completed_at = NOW()
               WHERE source_record_id = $1 AND status = 'pending'
                 AND deleted_at IS NULL""",
            rid,
        )

    await audit_log("resolve", get_user_email(request),
                    "structured_records", recall_id,
                    {"record_type": "vehicle_recall",
                     "service_record_id": body.service_record_id})
    return {"data": {"id": recall_id, "data": cleaned}}


@router.post("/vehicles/{vehicle_id}/recalls/refresh")
async def refresh_recalls(vehicle_id: str, request: Request):
    """Hit NHTSA for this vehicle and ingest any new campaigns. Returns
    counts so the UI can show "Found 2 new recalls" or "All clear"."""
    try:
        vid = uuid.UUID(vehicle_id)
    except ValueError:
        raise HTTPException(400, "Invalid vehicle id")

    pool = get_pool()
    async with pool.acquire() as conn:
        vehicle = await _fetch_vehicle(conn, vid)
        vdata = _data(vehicle)
        make = vdata.get("make")
        model = vdata.get("model")
        year = vdata.get("year")
        if not (make and model and year):
            raise HTTPException(
                400, "Vehicle needs make, model, and year for NHTSA lookup",
            )

        try:
            payload = await fetch_recalls_for_vehicle(make, model, int(year))
        except httpx.HTTPError as e:
            logger.warning(f"NHTSA call failed for vehicle {vid}: {e}")
            raise HTTPException(502, f"NHTSA unreachable: {e}")
        except (ValueError, TypeError) as e:
            raise HTTPException(400, f"Invalid vehicle year: {year} ({e})")

        parsed = parse_nhtsa_results(payload, str(vid))

        existing_rows = await conn.fetch(
            """SELECT data->>'nhtsa_campaign_number' AS campaign
               FROM structured_records
               WHERE record_type = 'vehicle_recall' AND deleted_at IS NULL
                 AND data->>'vehicle_record_id' = $1""",
            str(vid),
        )
        existing = {r["campaign"] for r in existing_rows if r["campaign"]}
        new_recalls = filter_new_recalls(parsed, existing)

        created_ids: list[str] = []
        for blob in new_recalls:
            cleaned = validate_record("vehicle_recall", blob)
            row = await conn.fetchrow(
                """INSERT INTO structured_records
                       (record_type, domain, subject_id, data)
                   VALUES ('vehicle_recall', 'auto', $1, $2)
                   RETURNING id""",
                vehicle["subject_id"], cleaned,
            )
            created_ids.append(str(row["id"]))
            # Action item so the recall shows up in pending lists / dashboard.
            await conn.execute(
                """INSERT INTO action_items
                       (domain, subject_id, title, description,
                        source_type, source_record_id, priority)
                   VALUES ('auto', $1, $2, $3, 'recall', $4, 'high')""",
                vehicle["subject_id"],
                f"Recall: {blob.get('component') or 'Vehicle recall'}",
                blob.get("summary"),
                row["id"],
            )

    await audit_log("refresh_recalls", get_user_email(request),
                    "structured_records", vehicle_id,
                    {"checked": len(parsed), "new": len(created_ids)})
    return {"data": {
        "vehicle_id": vehicle_id,
        "checked": len(parsed),
        "new_recall_ids": created_ids,
        "new_count": len(created_ids),
    }}
