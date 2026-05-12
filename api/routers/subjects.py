"""
routers/subjects.py — Subject CRUD.
"""

import logging
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Request

from database import get_pool
from helpers import get_user_email, audit_log
from models import SubjectCreate, SubjectUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/subjects", tags=["subjects"])


# ── Pet license renewal action item ─────────────────────────────────────

async def _ensure_pet_license_action(conn, subject_id, name: str, profile: dict):
    """If a pet's license_expiration is set, ensure a renewal action exists.

    Surfaces 60 days before expiration. Idempotent: re-running for the same
    expiration date doesn't stack duplicates.
    """
    raw = (profile or {}).get("license_expiration")
    if not raw:
        return
    try:
        exp = date.fromisoformat(str(raw))
    except (ValueError, TypeError):
        return

    today = date.today()
    if today < exp - timedelta(days=60):
        return  # too far out, don't nag yet

    # Dedup on (subject_id, due_date) — re-running with the same expiration
    # date is a no-op.
    existing = await conn.fetchval("""
        SELECT 1 FROM action_items
        WHERE subject_id = $1
          AND title = $2
          AND due_date = $3
          AND deleted_at IS NULL
        LIMIT 1
    """, subject_id, f"Renew pet license: {name}", exp)
    if existing:
        return

    priority = "high" if exp < today else "medium"
    await conn.execute("""
        INSERT INTO action_items
            (domain, subject_id, title, description, due_date, source_type, priority)
        VALUES ('vet', $1, $2, $3, $4, 'recurring', $5)
    """,
        subject_id,
        f"Renew pet license: {name}",
        (profile or {}).get("license_number") and f"License #{profile['license_number']}" or None,
        exp,
        priority,
    )


@router.get("")
async def list_subjects():
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT s.*,
                (SELECT COUNT(*) FROM documents d
                 WHERE d.subject_id = s.id AND d.deleted_at IS NULL) as document_count
            FROM subjects s
            WHERE s.deleted_at IS NULL
            ORDER BY s.is_primary DESC, s.name
        """)

    return {
        "data": [
            {
                "id": str(r["id"]),
                "name": r["name"],
                "type": r["type"],
                "profile_data": r["profile_data"] if isinstance(r["profile_data"], dict) else {},
                "is_primary": r["is_primary"],
                "document_count": r["document_count"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]
    }


@router.post("")
async def create_subject(body: SubjectCreate, request: Request):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO subjects (name, type, profile_data, is_primary)
            VALUES ($1, $2, $3, $4) RETURNING *
        """, body.name, body.type, body.profile_data, body.is_primary)

        if row["type"] == "pet":
            try:
                await _ensure_pet_license_action(
                    conn, row["id"], row["name"],
                    row["profile_data"] if isinstance(row["profile_data"], dict) else {},
                )
            except Exception as e:
                logger.warning(f"Pet license action failed for {row['id']}: {e}")

    await audit_log("create", get_user_email(request), "subjects", str(row["id"]))
    return {
        "data": {
            "id": str(row["id"]),
            "name": row["name"],
            "type": row["type"],
            "profile_data": row["profile_data"] if isinstance(row["profile_data"], dict) else {},
            "is_primary": row["is_primary"],
            "created_at": row["created_at"].isoformat(),
        }
    }


@router.get("/{subject_id}")
async def get_subject(subject_id: str):
    pool = get_pool()
    sid = uuid.UUID(subject_id)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM subjects WHERE id = $1 AND deleted_at IS NULL", sid
        )
        if not row:
            raise HTTPException(404, "Subject not found")

        docs = await conn.fetch("""
            SELECT id, title, domain, category, ingested_at
            FROM documents
            WHERE subject_id = $1 AND deleted_at IS NULL
            ORDER BY ingested_at DESC LIMIT 20
        """, sid)

    return {
        "data": {
            "id": str(row["id"]),
            "name": row["name"],
            "type": row["type"],
            "profile_data": row["profile_data"] if isinstance(row["profile_data"], dict) else {},
            "is_primary": row["is_primary"],
            "created_at": row["created_at"].isoformat(),
            "recent_documents": [
                {
                    "id": str(d["id"]),
                    "title": d["title"],
                    "domain": d["domain"],
                    "category": d["category"],
                    "ingested_at": d["ingested_at"].isoformat() if d["ingested_at"] else None,
                }
                for d in docs
            ],
        }
    }


@router.patch("/{subject_id}")
async def update_subject(subject_id: str, body: SubjectUpdate, request: Request):
    pool = get_pool()
    sid = uuid.UUID(subject_id)
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT * FROM subjects WHERE id = $1 AND deleted_at IS NULL", sid
        )
        if not existing:
            raise HTTPException(404, "Subject not found")

        updated = await conn.fetchrow("""
            UPDATE subjects SET
                name = COALESCE($2, name),
                type = COALESCE($3, type),
                profile_data = COALESCE($4, profile_data)
            WHERE id = $1
            RETURNING *
        """, sid, body.name, body.type, body.profile_data)

        if updated["type"] == "pet":
            try:
                await _ensure_pet_license_action(
                    conn, sid, updated["name"],
                    updated["profile_data"] if isinstance(updated["profile_data"], dict) else {},
                )
            except Exception as e:
                logger.warning(f"Pet license action failed for {sid}: {e}")

    await audit_log("update", get_user_email(request), "subjects", subject_id)
    return {"data": {"id": subject_id, "updated": True}}
