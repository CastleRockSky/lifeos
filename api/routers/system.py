"""
routers/system.py — Health, stats, domains, categories, metrics.
"""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, HTTPException
from qdrant_client import QdrantClient

from config import get_settings
from database import get_pool
from constants import DOMAINS, CATEGORIES

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
async def health_check():
    pool = get_pool()
    db_ok = False
    qdrant_ok = False

    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_ok = True
    except Exception:
        pass

    try:
        client = QdrantClient(url=get_settings().qdrant_url, timeout=5)
        client.get_collections()
        qdrant_ok = True
    except Exception:
        pass

    status = "healthy" if db_ok and qdrant_ok else "degraded"
    return {
        "status": status,
        "database": "ok" if db_ok else "error",
        "qdrant": "ok" if qdrant_ok else "error",
    }


@router.get("/stats")
async def get_stats():
    pool = get_pool()
    async with pool.acquire() as conn:
        total_docs = await conn.fetchval(
            "SELECT COUNT(*) FROM documents WHERE deleted_at IS NULL"
        )
        total_size = await conn.fetchval(
            "SELECT COALESCE(SUM(file_size_bytes), 0) FROM documents WHERE deleted_at IS NULL"
        )
        total_chunks = await conn.fetchval("SELECT COUNT(*) FROM document_chunks")
        total_subjects = await conn.fetchval(
            "SELECT COUNT(*) FROM subjects WHERE deleted_at IS NULL"
        )
        domain_rows = await conn.fetch("""
            SELECT domain, COUNT(*) as count
            FROM documents WHERE deleted_at IS NULL AND domain IS NOT NULL
            GROUP BY domain ORDER BY count DESC
        """)
        pending_actions = await conn.fetchval(
            "SELECT COUNT(*) FROM action_items WHERE status = 'pending' AND deleted_at IS NULL"
        )
        needs_review = await conn.fetchval(
            "SELECT COUNT(*) FROM documents WHERE review_status = 'needs_review' AND deleted_at IS NULL"
        )

    return {
        "data": {
            "documents": total_docs,
            "storage_bytes": total_size,
            "chunks": total_chunks,
            "subjects": total_subjects,
            "by_domain": {r["domain"]: r["count"] for r in domain_rows},
            "pending_actions": pending_actions,
            "needs_review": needs_review,
        }
    }


@router.get("/calendar/status")
async def calendar_status():
    """Google Calendar sync status (Phase 10)."""
    from calendar_sync import status
    return {"data": status()}


@router.get("/inbox/status")
async def inbox_status():
    """Return inbox watcher stats."""
    settings = get_settings()
    if not settings.inbox_enabled:
        return {"data": {"enabled": False}}

    from inbox_watcher import get_inbox_stats
    return {"data": {"enabled": True, **get_inbox_stats()}}


@router.get("/domains")
async def list_domains():
    return {"data": DOMAINS}


@router.get("/categories")
async def list_categories(domain: str = Query(None)):
    if domain:
        if domain not in CATEGORIES:
            raise HTTPException(400, f"Invalid domain: {domain}")
        return {"data": CATEGORIES[domain]}
    return {"data": CATEGORIES}


@router.get("/metrics")
async def list_metrics(
    subject_id: str = Query(None),
    metric_type: str = Query(None),
    days: int = Query(90, ge=1, le=3650),
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=1000),
):
    """Query time-series metrics with optional filters."""
    pool = get_pool()
    conditions = []
    params = []
    idx = 0

    if subject_id:
        idx += 1
        conditions.append(f"m.subject_id = ${idx}")
        params.append(uuid.UUID(subject_id))
    if metric_type:
        idx += 1
        conditions.append(f"m.metric_type = ${idx}")
        params.append(metric_type)

    idx += 1
    conditions.append(f"m.recorded_at >= ${idx}")
    params.append(datetime.now(timezone.utc) - timedelta(days=days))

    where = " AND ".join(conditions)
    offset = (page - 1) * per_page

    async with pool.acquire() as conn:
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM time_series_metrics m WHERE {where}", *params
        )

        idx += 1; params.append(per_page); limit_idx = idx
        idx += 1; params.append(offset); offset_idx = idx

        rows = await conn.fetch(f"""
            SELECT m.id, m.subject_id, s.name as subject_name,
                   m.metric_type, m.value_numeric, m.value_text,
                   m.recorded_at, m.source, m.source_document_id, m.notes
            FROM time_series_metrics m
            LEFT JOIN subjects s ON s.id = m.subject_id
            WHERE {where}
            ORDER BY m.recorded_at DESC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
        """, *params)

    return {
        "data": [
            {
                "id": r["id"],
                "subject_id": str(r["subject_id"]),
                "subject_name": r["subject_name"],
                "metric_type": r["metric_type"],
                "value_numeric": float(r["value_numeric"]) if r["value_numeric"] is not None else None,
                "value_text": r["value_text"],
                "recorded_at": r["recorded_at"].isoformat() if r["recorded_at"] else None,
                "source": r["source"],
                "source_document_id": str(r["source_document_id"]) if r["source_document_id"] else None,
                "notes": r["notes"],
            }
            for r in rows
        ],
        "meta": {"total": total, "page": page, "per_page": per_page},
    }
