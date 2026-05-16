"""
routers/system.py — Health, stats, domains, categories, metrics, config, backups.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, HTTPException
from qdrant_client import QdrantClient

from config import get_settings
from database import get_pool
from constants import DOMAINS, CATEGORIES

router = APIRouter(prefix="/api", tags=["system"])

# ── Settings page support ───────────────────────────────────────────────
#
# The Settings UI is read-only: it surfaces the effective config (secrets
# masked) and the backup tarballs. Editing still happens in .env + restart.

# Setting keys whose values must never leave the server. Shown as a
# set/not-set indicator instead of the literal value.
_SECRET_SETTINGS = {
    "anthropic_api_key",
    "secret_key",
    "imap_password",
    "database_url",  # embeds the Postgres password
}

# Curated, ordered view of config — (group, [setting keys]). Keys absent from
# the Settings model are skipped, so this is safe across config changes.
_CONFIG_GROUPS = [
    ("AI & embeddings", ["anthropic_api_key", "embedding_model", "embedding_dim",
                         "chunk_size", "chunk_overlap"]),
    ("Storage", ["upload_dir", "backup_dir", "qdrant_url", "qdrant_collection"]),
    ("Security", ["secret_key", "allowed_origins"]),
    ("Inbox watcher", ["inbox_enabled", "inbox_dir", "inbox_poll_interval",
                       "inbox_stability_seconds"]),
    ("Email ingestion", ["imap_enabled", "imap_host", "imap_port", "imap_username",
                         "imap_password", "imap_mailbox", "imap_poll_interval"]),
    ("Google Calendar", ["google_calendar_enabled", "google_calendar_id",
                         "google_calendar_domains", "google_credentials_path"]),
    ("Hardening", ["max_upload_bytes", "qa_rate_limit_per_minute"]),
]


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


@router.get("/system/config")
async def get_config():
    """Effective runtime config for the Settings page (read-only).

    Secrets are never returned — only a set/not-set flag. Also reports
    derived feature-status booleans so the UI can show what's actually live.
    """
    s = get_settings()
    dump = s.model_dump()

    groups = []
    for group_name, keys in _CONFIG_GROUPS:
        items = []
        for key in keys:
            if key not in dump:
                continue
            value = dump[key]
            is_secret = key in _SECRET_SETTINGS
            items.append({
                "key": key,
                "label": key.replace("_", " "),
                "secret": is_secret,
                # For secrets, report only whether a non-default value is set.
                "value": (bool(value) and value != "change-me") if is_secret else value,
            })
        if items:
            groups.append({"group": group_name, "settings": items})

    features = {
        "inbox_watcher": s.inbox_enabled,
        "email_ingestion": s.imap_enabled,
        "google_calendar": s.google_calendar_enabled,
        "anthropic_api": bool(s.anthropic_api_key),
        "cors_locked_down": s.allowed_origins not in ("", "*"),
    }
    return {"data": {"features": features, "groups": groups}}


@router.get("/system/backups")
async def list_backups():
    """List backup tarballs (scripts/backup.sh output), newest first.

    The backup directory is mounted read-only into the API container. If the
    mount is missing, `accessible` is false rather than erroring.
    """
    backup_dir = get_settings().backup_dir
    result = {
        "backup_dir": backup_dir,
        "accessible": False,
        "backups": [],
        "count": 0,
        "total_bytes": 0,
        "newest_age_hours": None,
    }

    if not os.path.isdir(backup_dir):
        return {"data": result}

    result["accessible"] = True
    entries = []
    for name in os.listdir(backup_dir):
        if not (name.startswith("lifeos-") and name.endswith(".tar.gz")):
            continue
        path = os.path.join(backup_dir, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        entries.append({
            "name": name,
            "size_bytes": st.st_size,
            "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        })

    entries.sort(key=lambda e: e["modified"], reverse=True)
    result["backups"] = entries
    result["count"] = len(entries)
    result["total_bytes"] = sum(e["size_bytes"] for e in entries)
    if entries:
        newest = datetime.fromisoformat(entries[0]["modified"])
        age = datetime.now(timezone.utc) - newest
        result["newest_age_hours"] = round(age.total_seconds() / 3600, 1)

    return {"data": result}


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
