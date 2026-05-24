"""
routers/records.py — Generic CRUD for structured_records (Phase 5).

Validates `data` against the per-record_type schema on write. Soft-deletes
on DELETE. Used by the medical dashboard, future domain dashboards, and
agent endpoints.
"""

import json
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ValidationError

from constants import DOMAINS
from database import get_pool
from helpers import audit_log, get_user_email
from schemas import known_record_types, validate_record

router = APIRouter(prefix="/api/records", tags=["records"])


class RecordCreate(BaseModel):
    record_type: str
    domain: Optional[str] = None
    subject_id: Optional[str] = None
    data: dict
    source_document_id: Optional[str] = None
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    next_action_date: Optional[datetime] = None
    next_action_description: Optional[str] = None


class RecordUpdate(BaseModel):
    domain: Optional[str] = None
    subject_id: Optional[str] = None
    data: Optional[dict] = None
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    next_action_date: Optional[datetime] = None
    next_action_description: Optional[str] = None


def _serialise(row) -> dict:
    return {
        "id": str(row["id"]),
        "record_type": row["record_type"],
        "domain": row["domain"],
        "subject_id": str(row["subject_id"]) if row["subject_id"] else None,
        "data": row["data"] if isinstance(row["data"], dict) else json.loads(row["data"]),
        "source_document_id": str(row["source_document_id"]) if row["source_document_id"] else None,
        "valid_from": row["valid_from"].isoformat() if row["valid_from"] else None,
        "valid_to": row["valid_to"].isoformat() if row["valid_to"] else None,
        "next_action_date": row["next_action_date"].isoformat() if row["next_action_date"] else None,
        "next_action_description": row["next_action_description"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


def _validate_or_400(record_type: str, data: dict) -> dict:
    try:
        return validate_record(record_type, data)
    except ValidationError as e:
        raise HTTPException(
            422,
            detail={
                "error": "schema_validation",
                "message": f"Invalid data for record_type '{record_type}'",
                "details": e.errors(),
            },
        )


@router.get("/types")
async def list_record_types():
    """Return the set of record_types that have a schema."""
    return {"data": known_record_types()}


@router.post("")
async def create_record(payload: RecordCreate, request: Request):
    if payload.domain and payload.domain not in DOMAINS:
        raise HTTPException(400, f"Invalid domain: {payload.domain}")

    cleaned = _validate_or_400(payload.record_type, payload.data)

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO structured_records (
                record_type, domain, subject_id, data, source_document_id,
                valid_from, valid_to, next_action_date, next_action_description
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING *
        """,
            payload.record_type,
            payload.domain,
            uuid.UUID(payload.subject_id) if payload.subject_id else None,
            cleaned,
            uuid.UUID(payload.source_document_id) if payload.source_document_id else None,
            payload.valid_from,
            payload.valid_to,
            payload.next_action_date,
            payload.next_action_description,
        )

        # Auto-generate the next pending action item for recurring records.
        if payload.record_type in (
            "credit_account", "loan", "recurring_expense",
            "appliance", "home_maintenance_schedule",
            "maintenance_schedule", "vehicle",
            "pet_vaccination", "preventative_schedule", "pet_medication",
            "insurance_policy", "identity_document",
            "tax_item",
        ):
            from recurrences import ensure_recurring_action_item
            await ensure_recurring_action_item(
                conn,
                record_type=payload.record_type,
                record_id=row["id"],
                data=cleaned,
                subject_id=row["subject_id"],
                source_document_id=row["source_document_id"],
            )

    await audit_log("create", get_user_email(request), "structured_records",
                    str(row["id"]), {"record_type": payload.record_type})
    return {"data": _serialise(row)}


@router.get("")
async def list_records(
    record_type: Optional[str] = Query(None),
    domain: Optional[str] = Query(None),
    subject_id: Optional[str] = Query(None),
    source_document_id: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
):
    pool = get_pool()
    conditions = ["deleted_at IS NULL"]
    params: list = []
    idx = 0

    def _add(cond: str, value):
        nonlocal idx
        idx += 1
        conditions.append(cond.format(idx))
        params.append(value)

    if record_type:
        _add("record_type = ${}", record_type)
    if domain:
        _add("domain = ${}", domain)
    if subject_id:
        _add("subject_id = ${}", uuid.UUID(subject_id))
    if source_document_id:
        _add("source_document_id = ${}", uuid.UUID(source_document_id))

    where = " AND ".join(conditions)
    offset = (page - 1) * per_page

    async with pool.acquire() as conn:
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM structured_records WHERE {where}", *params
        )
        idx += 1; params.append(per_page); limit_idx = idx
        idx += 1; params.append(offset); offset_idx = idx
        rows = await conn.fetch(
            f"""SELECT * FROM structured_records
                WHERE {where}
                ORDER BY updated_at DESC
                LIMIT ${limit_idx} OFFSET ${offset_idx}""",
            *params,
        )

    return {
        "data": [_serialise(r) for r in rows],
        "meta": {"total": total, "page": page, "per_page": per_page},
    }


@router.get("/{record_id}")
async def get_record(record_id: str):
    try:
        rid = uuid.UUID(record_id)
    except ValueError:
        raise HTTPException(400, "Invalid record id")

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM structured_records WHERE id = $1 AND deleted_at IS NULL",
            rid,
        )
    if not row:
        raise HTTPException(404, "Record not found")
    return {"data": _serialise(row)}


@router.patch("/{record_id}")
async def update_record(record_id: str, payload: RecordUpdate, request: Request):
    try:
        rid = uuid.UUID(record_id)
    except ValueError:
        raise HTTPException(400, "Invalid record id")

    pool = get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT record_type, data FROM structured_records WHERE id = $1 AND deleted_at IS NULL",
            rid,
        )
    if not existing:
        raise HTTPException(404, "Record not found")

    new_data = None
    if payload.data is not None:
        # Merge: payload data overrides existing keys, keeps the rest.
        merged = dict(existing["data"]) if isinstance(existing["data"], dict) else json.loads(existing["data"])
        merged.update(payload.data)
        new_data = _validate_or_400(existing["record_type"], merged)

    sets = []
    params: list = []
    idx = 0

    def _set(col: str, value):
        nonlocal idx
        idx += 1
        sets.append(f"{col} = ${idx}")
        params.append(value)

    if payload.domain is not None:
        if payload.domain and payload.domain not in DOMAINS:
            raise HTTPException(400, f"Invalid domain: {payload.domain}")
        _set("domain", payload.domain or None)
    if payload.subject_id is not None:
        _set("subject_id", uuid.UUID(payload.subject_id) if payload.subject_id else None)
    if new_data is not None:
        _set("data", new_data)
    if payload.valid_from is not None:
        _set("valid_from", payload.valid_from)
    if payload.valid_to is not None:
        _set("valid_to", payload.valid_to)
    if payload.next_action_date is not None:
        _set("next_action_date", payload.next_action_date)
    if payload.next_action_description is not None:
        _set("next_action_description", payload.next_action_description)

    if not sets:
        return await get_record(record_id)

    idx += 1; params.append(rid)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE structured_records SET {', '.join(sets)} WHERE id = ${idx} RETURNING *",
            *params,
        )
    await audit_log("update", get_user_email(request), "structured_records",
                    str(row["id"]), {"fields": list(payload.model_dump(exclude_none=True).keys())})
    return {"data": _serialise(row)}


@router.delete("/{record_id}")
async def delete_record(record_id: str, request: Request):
    try:
        rid = uuid.UUID(record_id)
    except ValueError:
        raise HTTPException(400, "Invalid record id")

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE structured_records SET deleted_at = NOW() "
            "WHERE id = $1 AND deleted_at IS NULL RETURNING id",
            rid,
        )
    if not row:
        raise HTTPException(404, "Record not found")
    await audit_log("delete", get_user_email(request), "structured_records", str(rid))
    return {"data": {"id": str(rid), "deleted": True}}
