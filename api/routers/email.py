"""
routers/email.py — Email forwarding ingestion status, queue, retry (Phase 3).
"""

import uuid

from fastapi import APIRouter, HTTPException, Query

from config import get_settings
from database import get_pool

router = APIRouter(prefix="/api/email", tags=["email"])


@router.get("/status")
async def email_status():
    """Watcher state + recent processing log."""
    settings = get_settings()
    if not settings.imap_enabled:
        return {"data": {"enabled": False}}

    from email_ingest import get_email_stats

    pool = get_pool()
    async with pool.acquire() as conn:
        recent = await conn.fetch("""
            SELECT id, sender, original_sender, subject, clean_subject,
                   received_at, processed_at, status, document_count,
                   attachment_count, error_message
            FROM email_messages
            ORDER BY COALESCE(processed_at, created_at) DESC
            LIMIT 25
        """)
        totals = await conn.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'processed') AS processed,
                COUNT(*) FILTER (WHERE status = 'partial')   AS partial,
                COUNT(*) FILTER (WHERE status = 'failed')    AS failed,
                COUNT(*) FILTER (WHERE status IN ('pending','processing')) AS pending,
                COUNT(*) AS total
            FROM email_messages
        """)

    return {
        "data": {
            "enabled": True,
            "watcher": get_email_stats(),
            "totals": dict(totals) if totals else {},
            "recent": [
                {
                    "id": str(r["id"]),
                    "sender": r["sender"],
                    "original_sender": r["original_sender"],
                    "subject": r["subject"],
                    "clean_subject": r["clean_subject"],
                    "received_at": r["received_at"].isoformat() if r["received_at"] else None,
                    "processed_at": r["processed_at"].isoformat() if r["processed_at"] else None,
                    "status": r["status"],
                    "document_count": r["document_count"],
                    "attachment_count": r["attachment_count"],
                    "error_message": r["error_message"],
                }
                for r in recent
            ],
        }
    }


@router.get("/queue")
async def email_queue(
    status: str = Query("failed", regex="^(pending|processing|failed|partial|all)$"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
):
    """List email_messages rows that are pending or have failed (default: failed)."""
    pool = get_pool()
    offset = (page - 1) * per_page

    if status == "all":
        where = "1 = 1"
        params: list = []
    else:
        where = "status = $1"
        params = [status]

    async with pool.acquire() as conn:
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM email_messages WHERE {where}", *params
        )
        rows = await conn.fetch(
            f"""
            SELECT id, sender, original_sender, subject, clean_subject,
                   received_at, processed_at, status, document_count,
                   attachment_count, error_message, retry_count
            FROM email_messages
            WHERE {where}
            ORDER BY COALESCE(processed_at, created_at) DESC
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """,
            *params, per_page, offset,
        )

    return {
        "data": [
            {
                "id": str(r["id"]),
                "sender": r["sender"],
                "original_sender": r["original_sender"],
                "subject": r["subject"],
                "clean_subject": r["clean_subject"],
                "received_at": r["received_at"].isoformat() if r["received_at"] else None,
                "processed_at": r["processed_at"].isoformat() if r["processed_at"] else None,
                "status": r["status"],
                "document_count": r["document_count"],
                "attachment_count": r["attachment_count"],
                "error_message": r["error_message"],
                "retry_count": r["retry_count"],
            }
            for r in rows
        ],
        "meta": {"total": total, "page": page, "per_page": per_page},
    }


@router.post("/retry/{email_id}")
async def email_retry(email_id: str):
    """Retry processing for a failed email row (body-only — re-forward for attachments)."""
    try:
        uuid.UUID(email_id)
    except ValueError:
        raise HTTPException(400, "Invalid email id")

    from email_ingest import retry_email

    result = await retry_email(email_id)
    if not result.get("ok") and result.get("error") == "not_found":
        raise HTTPException(404, "Email message not found")
    return {"data": result}


@router.get("/senders")
async def list_senders(page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=200)):
    """List sender→domain mappings (auto-learned + manual)."""
    pool = get_pool()
    offset = (page - 1) * per_page
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM email_sender_map")
        rows = await conn.fetch("""
            SELECT id, sender_pattern, domain, category, subject_hint,
                   auto_learned, confidence, match_count, last_matched_at
            FROM email_sender_map
            ORDER BY match_count DESC, confidence DESC
            LIMIT $1 OFFSET $2
        """, per_page, offset)

    return {
        "data": [
            {
                "id": str(r["id"]),
                "sender_pattern": r["sender_pattern"],
                "domain": r["domain"],
                "category": r["category"],
                "subject_hint": r["subject_hint"],
                "auto_learned": r["auto_learned"],
                "confidence": float(r["confidence"]) if r["confidence"] is not None else 0.0,
                "match_count": r["match_count"],
                "last_matched_at": r["last_matched_at"].isoformat() if r["last_matched_at"] else None,
            }
            for r in rows
        ],
        "meta": {"total": total, "page": page, "per_page": per_page},
    }
