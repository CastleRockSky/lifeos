"""
routers/actions.py — Action items CRUD.
"""

import json
import uuid
from datetime import date, timedelta, datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Query, HTTPException, Request

from database import get_pool
from helpers import get_user_email, audit_log
from models import ActionItemCreate, ActionItemUpdate

router = APIRouter(prefix="/api/actions", tags=["actions"])


@router.get("")
async def list_actions(
    status: str = Query(None),
    domain: str = Query(None),
    due_before: str = Query(None),
    due_after: str = Query(None),
    overdue: bool = Query(False),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
):
    """List action items with filters."""
    pool = get_pool()
    conditions = ["a.deleted_at IS NULL"]
    params = []
    idx = 0

    if status:
        idx += 1; conditions.append(f"a.status = ${idx}"); params.append(status)
    if domain:
        idx += 1; conditions.append(f"a.domain = ${idx}"); params.append(domain)
    if due_before:
        idx += 1; conditions.append(f"a.due_date <= ${idx}"); params.append(date.fromisoformat(due_before))
    if due_after:
        idx += 1; conditions.append(f"a.due_date >= ${idx}"); params.append(date.fromisoformat(due_after))
    if overdue:
        idx += 1; conditions.append(f"a.due_date < ${idx}"); params.append(date.today())
        conditions.append("a.status NOT IN ('completed', 'dismissed')")

    where = " AND ".join(conditions)
    offset = (page - 1) * per_page

    async with pool.acquire() as conn:
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM action_items a WHERE {where}", *params
        )

        idx += 1; params.append(per_page); limit_idx = idx
        idx += 1; params.append(offset); offset_idx = idx

        rows = await conn.fetch(f"""
            SELECT a.*, d.title as document_title, s.name as subject_name
            FROM action_items a
            LEFT JOIN documents d ON d.id = a.source_document_id
            LEFT JOIN subjects s ON s.id = a.subject_id
            WHERE {where}
            ORDER BY
                CASE WHEN a.status = 'pending' THEN 0 WHEN a.status = 'in_progress' THEN 1 ELSE 2 END,
                a.due_date NULLS LAST,
                a.created_at DESC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
        """, *params)

    return {
        "data": [_action_dict(r) for r in rows],
        "meta": {"total": total, "page": page, "per_page": per_page},
    }


@router.get("/upcoming")
async def upcoming_actions(days: int = Query(30, ge=1, le=365)):
    """Action items due in the next N days."""
    pool = get_pool()
    cutoff = date.today() + timedelta(days=days)

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT a.*, d.title as document_title, s.name as subject_name
            FROM action_items a
            LEFT JOIN documents d ON d.id = a.source_document_id
            LEFT JOIN subjects s ON s.id = a.subject_id
            WHERE a.deleted_at IS NULL
                AND a.status NOT IN ('completed', 'dismissed')
                AND a.due_date IS NOT NULL
                AND a.due_date <= $1
            ORDER BY a.due_date ASC
        """, cutoff)

    return {"data": [_action_dict(r) for r in rows]}


@router.get("/overdue")
async def overdue_actions():
    """Past-due action items."""
    pool = get_pool()
    today = date.today()

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT a.*, d.title as document_title, s.name as subject_name
            FROM action_items a
            LEFT JOIN documents d ON d.id = a.source_document_id
            LEFT JOIN subjects s ON s.id = a.subject_id
            WHERE a.deleted_at IS NULL
                AND a.status NOT IN ('completed', 'dismissed')
                AND a.due_date IS NOT NULL
                AND a.due_date < $1
            ORDER BY a.due_date ASC
        """, today)

    return {"data": [_action_dict(r) for r in rows]}


@router.post("")
async def create_action(body: ActionItemCreate, request: Request, background_tasks: BackgroundTasks):
    """Manually create an action item."""
    pool = get_pool()

    due = None
    if body.due_date:
        try:
            due = date.fromisoformat(body.due_date)
        except ValueError:
            raise HTTPException(400, "Invalid due_date format. Use YYYY-MM-DD.")

    sid = uuid.UUID(body.subject_id) if body.subject_id else None

    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO action_items (title, description, domain, subject_id, due_date, priority, source_type)
            VALUES ($1, $2, $3, $4, $5, $6, 'manual')
            RETURNING *
        """, body.title, body.description, body.domain, sid, due, body.priority)

    await audit_log("create", get_user_email(request), "action_items", str(row["id"]))

    # Mirror to Google Calendar (no-op if disabled).
    background_tasks.add_task(_sync_calendar_for_action, str(row["id"]), "create")

    return {"data": _action_dict(row)}


@router.patch("/{action_id}")
async def update_action(action_id: str, body: ActionItemUpdate, request: Request,
                        background_tasks: BackgroundTasks):
    """Update an action item (status, due_date, notes, etc.)."""
    pool = get_pool()
    aid = uuid.UUID(action_id)

    valid_statuses = {"pending", "in_progress", "completed", "snoozed", "dismissed"}
    if body.status and body.status not in valid_statuses:
        raise HTTPException(400, f"Invalid status. Must be one of: {', '.join(valid_statuses)}")

    due = None
    if body.due_date:
        try:
            due = date.fromisoformat(body.due_date)
        except ValueError:
            raise HTTPException(400, "Invalid due_date format. Use YYYY-MM-DD.")

    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id, recurrence_rule, source_record_id "
            "FROM action_items WHERE id = $1 AND deleted_at IS NULL", aid
        )
        if not existing:
            raise HTTPException(404, "Action item not found")

        completed_at = None
        if body.status == "completed":
            completed_at = datetime.now(timezone.utc)

        await conn.execute("""
            UPDATE action_items SET
                status = COALESCE($2, status),
                due_date = COALESCE($3, due_date),
                description = COALESCE($4, description),
                title = COALESCE($5, title),
                priority = COALESCE($6, priority),
                completed_at = COALESCE($7, completed_at)
            WHERE id = $1
        """, aid, body.status, due, body.notes, body.title, body.priority, completed_at)

        # If a recurring action just got completed, queue the next occurrence.
        if (
            body.status == "completed"
            and existing["recurrence_rule"]
            and existing["source_record_id"]
        ):
            rec = await conn.fetchrow("""
                SELECT id, record_type, data, subject_id, source_document_id
                FROM structured_records WHERE id = $1 AND deleted_at IS NULL
            """, existing["source_record_id"])
            if rec:
                from recurrences import ensure_recurring_action_item
                rec_data = rec["data"] if isinstance(rec["data"], dict) else None
                if rec_data is None:
                    import json as _json
                    rec_data = _json.loads(rec["data"])
                await ensure_recurring_action_item(
                    conn,
                    record_type=rec["record_type"],
                    record_id=rec["id"],
                    data=rec_data,
                    subject_id=rec["subject_id"],
                    source_document_id=rec["source_document_id"],
                )

    await audit_log("update", get_user_email(request), "action_items", action_id)

    # Mirror status to Google Calendar.
    if body.status in ("completed", "dismissed"):
        background_tasks.add_task(_sync_calendar_for_action, action_id, "delete")
    else:
        background_tasks.add_task(_sync_calendar_for_action, action_id, "update")

    return {"data": {"id": action_id, "updated": True}}


async def _sync_calendar_for_action(action_id: str, op: str):
    """Background task: re-read the action and apply the right calendar op."""
    from calendar_sync import sync_action_create, sync_action_update, sync_action_delete

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT a.*, s.name AS subject_name
            FROM action_items a
            LEFT JOIN subjects s ON s.id = a.subject_id
            WHERE a.id = $1
        """, uuid.UUID(action_id))
    if not row:
        return

    action = {
        "id": str(row["id"]),
        "title": row["title"],
        "description": row["description"],
        "due_date": row["due_date"],
        "priority": row["priority"],
        "domain": row["domain"],
        "subject_name": row.get("subject_name"),
        "recurrence_rule": row["recurrence_rule"],
    }
    event_id = row["calendar_event_id"]

    if op == "delete" and event_id:
        await sync_action_delete(event_id)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE action_items SET calendar_event_id = NULL WHERE id = $1",
                uuid.UUID(action_id),
            )
        return

    if op == "create":
        new_id = await sync_action_create(action)
    else:
        new_id = await sync_action_update(action, event_id)

    if new_id and new_id != event_id:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE action_items SET calendar_event_id = $1 WHERE id = $2",
                new_id, uuid.UUID(action_id),
            )


def _action_dict(r) -> dict:
    metadata = r["metadata"] if "metadata" in r.keys() else None
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = None
    return {
        "id": str(r["id"]),
        "title": r["title"],
        "description": r["description"],
        "domain": r["domain"],
        "subject_id": str(r["subject_id"]) if r["subject_id"] else None,
        "subject_name": r.get("subject_name"),
        "due_date": r["due_date"].isoformat() if r["due_date"] else None,
        "status": r["status"],
        "priority": r["priority"],
        "source_type": r["source_type"],
        "source_document_id": str(r["source_document_id"]) if r["source_document_id"] else None,
        "source_record_id": str(r["source_record_id"]) if r["source_record_id"] else None,
        "document_title": r.get("document_title"),
        "created_at": r["created_at"].isoformat(),
        "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
        "is_overdue": r["due_date"] is not None and r["due_date"] < date.today() and r["status"] not in ("completed", "dismissed"),
        "metadata": metadata or {},
    }
