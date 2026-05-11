"""
routers/documents.py — Document upload, CRUD, file serving, AI integration.
"""

import asyncio
import os
import uuid
import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, Form, Query, HTTPException, Request
from fastapi.responses import FileResponse

from database import get_pool
from constants import DOMAINS, ALL_CATEGORIES
from helpers import get_user_email, audit_log
from models import DocumentUpdate
from search import delete_document_vectors
from ingest import ingest_file, run_ai_analysis

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/documents", tags=["documents"])


# ── Upload ──────────────────────────────────────────────────────────────

@router.post("/upload")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(None),
    domain: str = Form(None),
    category: str = Form(None),
    subject_id: str = Form(None),
    tags: str = Form(""),
):
    user_email = get_user_email(request)

    if domain and domain not in DOMAINS:
        raise HTTPException(400, f"Invalid domain: {domain}")
    if category and category not in ALL_CATEGORIES:
        raise HTTPException(400, f"Invalid category: {category}")

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    # Write upload to a temp file, then run through shared pipeline
    ext = Path(file.filename).suffix.lower() if file.filename else ""
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        result = await ingest_file(
            tmp_path,
            original_filename=file.filename,
            title=title or file.filename or "Untitled",
            domain=domain,
            category=category,
            subject_id=subject_id,
            tags=tag_list,
            source="upload",
            uploaded_by=user_email,
        )
    finally:
        os.unlink(tmp_path)

    await audit_log("upload", user_email, "documents", result["id"],
                    {"filename": file.filename, "size": result["file_size_bytes"]})

    return {"data": result}


# ── Document CRUD ──────────────────────────────────────────────────────

@router.get("/review")
async def review_queue(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    """List documents needing review (low confidence or failed AI)."""
    pool = get_pool()
    offset = (page - 1) * per_page

    async with pool.acquire() as conn:
        total = await conn.fetchval("""
            SELECT COUNT(*) FROM documents
            WHERE deleted_at IS NULL AND (review_status = 'needs_review' OR ai_status = 'failed')
        """)

        rows = await conn.fetch("""
            SELECT d.id, d.title, d.domain, d.category, d.ai_status,
                   d.ai_confidence, d.review_status, d.ingested_at,
                   d.ai_summary, d.original_filename
            FROM documents d
            WHERE d.deleted_at IS NULL AND (d.review_status = 'needs_review' OR d.ai_status = 'failed')
            ORDER BY d.ingested_at DESC
            LIMIT $1 OFFSET $2
        """, per_page, offset)

    return {
        "data": [
            {
                "id": str(r["id"]),
                "title": r["title"],
                "domain": r["domain"],
                "category": r["category"],
                "ai_status": r["ai_status"],
                "ai_confidence": r["ai_confidence"],
                "review_status": r["review_status"],
                "ai_summary": r["ai_summary"],
                "original_filename": r["original_filename"],
                "ingested_at": r["ingested_at"].isoformat() if r["ingested_at"] else None,
            }
            for r in rows
        ],
        "meta": {"total": total, "page": page, "per_page": per_page},
    }


@router.get("")
async def list_documents(
    domain: str = Query(None),
    category: str = Query(None),
    subject_id: str = Query(None),
    q: str = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    pool = get_pool()
    conditions = ["d.deleted_at IS NULL"]
    params = []
    idx = 0

    if domain:
        idx += 1; conditions.append(f"d.domain = ${idx}"); params.append(domain)
    if category:
        idx += 1; conditions.append(f"d.category = ${idx}"); params.append(category)
    if subject_id:
        idx += 1; conditions.append(f"d.subject_id = ${idx}"); params.append(uuid.UUID(subject_id))
    if q:
        idx += 1
        conditions.append(f"""
            to_tsvector('english', COALESCE(d.title, '') || ' ' || COALESCE(d.content_text, ''))
            @@ plainto_tsquery('english', ${idx})
        """)
        params.append(q)

    where = " AND ".join(conditions)
    offset = (page - 1) * per_page

    async with pool.acquire() as conn:
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM documents d WHERE {where}", *params
        )

        idx += 1; params.append(per_page); limit_idx = idx
        idx += 1; params.append(offset); offset_idx = idx

        rows = await conn.fetch(f"""
            SELECT d.id, d.title, d.domain, d.category, d.file_type, d.mime_type,
                   d.file_size_bytes, d.text_length, d.ocr_applied, d.embedding_status,
                   d.ai_status, d.ai_confidence, d.tags, d.ingested_at,
                   d.original_filename, d.subject_id, s.name as subject_name
            FROM documents d
            LEFT JOIN subjects s ON s.id = d.subject_id
            WHERE {where}
            ORDER BY d.ingested_at DESC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
        """, *params)

    return {
        "data": [
            {
                "id": str(r["id"]),
                "title": r["title"],
                "domain": r["domain"],
                "category": r["category"],
                "file_type": r["file_type"],
                "mime_type": r["mime_type"],
                "file_size_bytes": r["file_size_bytes"],
                "text_length": r["text_length"],
                "ocr_applied": r["ocr_applied"],
                "embedding_status": r["embedding_status"],
                "ai_status": r["ai_status"],
                "ai_confidence": r["ai_confidence"],
                "tags": list(r["tags"]) if r["tags"] else [],
                "original_filename": r["original_filename"],
                "subject_id": str(r["subject_id"]) if r["subject_id"] else None,
                "subject_name": r["subject_name"],
                "ingested_at": r["ingested_at"].isoformat() if r["ingested_at"] else None,
            }
            for r in rows
        ],
        "meta": {"total": total, "page": page, "per_page": per_page},
    }


@router.get("/{document_id}")
async def get_document(document_id: str):
    pool = get_pool()
    did = uuid.UUID(document_id)
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT d.*, s.name as subject_name
            FROM documents d
            LEFT JOIN subjects s ON s.id = d.subject_id
            WHERE d.id = $1 AND d.deleted_at IS NULL
        """, did)
        if not row:
            raise HTTPException(404, "Document not found")

        chunks = await conn.fetch("""
            SELECT chunk_index, chunk_text, char_start, char_end
            FROM document_chunks WHERE document_id = $1
            ORDER BY chunk_index
        """, did)

        # Get related action items
        actions = await conn.fetch("""
            SELECT id, title, description, due_date, status, priority, created_at
            FROM action_items
            WHERE source_document_id = $1 AND deleted_at IS NULL
            ORDER BY due_date NULLS LAST
        """, did)

    return {
        "data": {
            "id": str(row["id"]),
            "title": row["title"],
            "domain": row["domain"],
            "category": row["category"],
            "file_type": row["file_type"],
            "mime_type": row["mime_type"],
            "file_size_bytes": row["file_size_bytes"],
            "file_path": row["file_path"],
            "original_filename": row["original_filename"],
            "content_text": row["content_text"],
            "text_length": row["text_length"],
            "ocr_applied": row["ocr_applied"],
            "ocr_confidence": row["ocr_confidence"],
            "page_count": row["page_count"],
            "embedding_status": row["embedding_status"],
            "ai_summary": row["ai_summary"],
            "ai_extracted_data": row["ai_extracted_data"],
            "ai_action_items": row["ai_action_items"],
            "ai_status": row["ai_status"],
            "ai_confidence": row["ai_confidence"],
            "review_status": row["review_status"],
            "document_date": row["document_date"].isoformat() if row["document_date"] else None,
            "expiration_date": row["expiration_date"].isoformat() if row["expiration_date"] else None,
            "ai_analyzed_at": row["ai_analyzed_at"].isoformat() if row["ai_analyzed_at"] else None,
            "tags": list(row["tags"]) if row["tags"] else [],
            "subject_id": str(row["subject_id"]) if row["subject_id"] else None,
            "subject_name": row["subject_name"],
            "uploaded_by": row["uploaded_by"],
            "ingested_at": row["ingested_at"].isoformat() if row["ingested_at"] else None,
            "created_at": row["created_at"].isoformat(),
            "chunks": [
                {
                    "index": c["chunk_index"],
                    "text": c["chunk_text"][:200],
                    "char_start": c["char_start"],
                    "char_end": c["char_end"],
                }
                for c in chunks
            ],
            "action_items": [
                {
                    "id": str(a["id"]),
                    "title": a["title"],
                    "description": a["description"],
                    "due_date": a["due_date"].isoformat() if a["due_date"] else None,
                    "status": a["status"],
                    "priority": a["priority"],
                    "created_at": a["created_at"].isoformat(),
                }
                for a in actions
            ],
        }
    }


@router.get("/{document_id}/file")
async def download_document(
    document_id: str,
    request: Request,
    download: bool = Query(False),
):
    pool = get_pool()
    did = uuid.UUID(document_id)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT file_path, original_filename, mime_type FROM documents WHERE id = $1 AND deleted_at IS NULL",
            did,
        )
    if not row:
        raise HTTPException(404, "Document not found")
    if not os.path.exists(row["file_path"]):
        raise HTTPException(404, "File not found on disk")

    await audit_log("download", get_user_email(request), "documents", document_id)
    return FileResponse(
        row["file_path"],
        filename=row["original_filename"],
        media_type=row["mime_type"],
        content_disposition_type="attachment" if download else "inline",
    )


@router.patch("/{document_id}")
async def update_document(document_id: str, body: DocumentUpdate, request: Request):
    pool = get_pool()
    did = uuid.UUID(document_id)

    if body.domain and body.domain not in DOMAINS:
        raise HTTPException(400, f"Invalid domain: {body.domain}")
    if body.category and body.category not in ALL_CATEGORIES:
        raise HTTPException(400, f"Invalid category: {body.category}")
    if body.review_status and body.review_status not in ("none", "needs_review", "reviewed"):
        raise HTTPException(400, f"Invalid review_status: {body.review_status}")

    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id FROM documents WHERE id = $1 AND deleted_at IS NULL", did
        )
        if not existing:
            raise HTTPException(404, "Document not found")

        sid = uuid.UUID(body.subject_id) if body.subject_id else None
        await conn.execute("""
            UPDATE documents SET
                title = COALESCE($2, title),
                domain = COALESCE($3, domain),
                category = COALESCE($4, category),
                subject_id = COALESCE($5, subject_id),
                tags = COALESCE($6, tags),
                review_status = COALESCE($7, review_status)
            WHERE id = $1
        """, did, body.title, body.domain, body.category, sid, body.tags, body.review_status)

    await audit_log("update", get_user_email(request), "documents", document_id)
    return {"data": {"id": document_id, "updated": True}}


@router.delete("/{document_id}")
async def delete_document(document_id: str, request: Request):
    pool = get_pool()
    did = uuid.UUID(document_id)
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id FROM documents WHERE id = $1 AND deleted_at IS NULL", did
        )
        if not existing:
            raise HTTPException(404, "Document not found")
        await conn.execute(
            "UPDATE documents SET deleted_at = NOW() WHERE id = $1", did
        )
        await conn.execute(
            "UPDATE action_items SET deleted_at = NOW() WHERE source_document_id = $1 AND deleted_at IS NULL", did
        )

    try:
        delete_document_vectors(document_id)
    except Exception as e:
        logger.warning(f"Failed to delete vectors for {document_id}: {e}")

    await audit_log("delete", get_user_email(request), "documents", document_id)
    return {"data": {"id": document_id, "deleted": True}}


@router.post("/{document_id}/reanalyze")
async def reanalyze_document(document_id: str, request: Request):
    """Trigger re-analysis for a single document."""
    pool = get_pool()
    did = uuid.UUID(document_id)

    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id, domain, category FROM documents WHERE id = $1 AND deleted_at IS NULL", did
        )
        if not existing:
            raise HTTPException(404, "Document not found")

        await conn.execute(
            "UPDATE documents SET ai_status = 'analyzing' WHERE id = $1", did
        )

    await audit_log("reanalyze", get_user_email(request), "documents", document_id)

    # Delete existing AI-extracted action items and metrics for this document
    async with pool.acquire() as conn:
        await conn.execute("""
            DELETE FROM action_items
            WHERE source_document_id = $1 AND source_type = 'ai_extracted'
        """, did)
        await conn.execute("""
            DELETE FROM time_series_metrics
            WHERE source_document_id = $1
        """, did)

    asyncio.create_task(run_ai_analysis(
        document_id, existing["domain"], existing["category"],
    ))

    return {"data": {"id": document_id, "ai_status": "analyzing"}}
