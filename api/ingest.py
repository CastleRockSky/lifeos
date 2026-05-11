"""
ingest.py — Shared document ingestion pipeline.

Used by both the upload endpoint and the inbox watcher.
"""

import asyncio
import os
import uuid
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import aiofiles
import magic

from config import get_settings
from database import get_pool
from processor import extract_text_with_metadata, chunk_text
from search import index_chunks

logger = logging.getLogger(__name__)


# ── Subject Resolution Helper ────────────────────────────────────────────

_DOMAIN_TO_SUBJECT_TYPE = {
    "vet": "pet",
    "auto": "vehicle",
    "home": "property",
}

_PRIMARY_FALLBACK_DOMAINS = {"medical", "financial", "legal", "insurance"}


async def _resolve_subject(pool, subject_hint: str | None, domain: str | None):
    """Match a subject_hint from AI analysis to a subjects row.

    Returns a UUID or None.
    """
    async with pool.acquire() as conn:
        # Try hint-based match first
        if subject_hint:
            expected_type = _DOMAIN_TO_SUBJECT_TYPE.get(domain)
            if expected_type:
                row = await conn.fetchrow(
                    "SELECT id FROM subjects WHERE deleted_at IS NULL AND type = $1 AND LOWER(name) LIKE $2 LIMIT 1",
                    expected_type,
                    f"%{subject_hint.lower()}%",
                )
            else:
                row = await conn.fetchrow(
                    "SELECT id FROM subjects WHERE deleted_at IS NULL AND LOWER(name) LIKE $1 LIMIT 1",
                    f"%{subject_hint.lower()}%",
                )
            if row:
                return row["id"]

        # Fallback to primary subject for personal domains
        if domain in _PRIMARY_FALLBACK_DOMAINS:
            row = await conn.fetchrow(
                "SELECT id FROM subjects WHERE deleted_at IS NULL AND is_primary = true LIMIT 1"
            )
            if row:
                return row["id"]

    return None


# ── Background AI Analysis ──────────────────────────────────────────────

async def run_ai_analysis(doc_id: str, domain: str = None, category: str = None):
    """Background task: run AI analysis on a document and update DB."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT content_text, title, mime_type, domain, category, subject_id FROM documents WHERE id = $1",
                uuid.UUID(doc_id),
            )
        if not row or not row["content_text"]:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE documents SET ai_status = 'skipped' WHERE id = $1",
                    uuid.UUID(doc_id),
                )
            return

        # Import here to avoid circular imports at module load
        from ai_analyzer import analyze_document

        existing_domain = domain or row["domain"]
        existing_category = category or row["category"]

        result = await analyze_document(
            text=row["content_text"],
            filename=row["title"],
            mime_type=row["mime_type"],
            existing_domain=existing_domain,
            existing_category=existing_category,
        )

        # Parse dates
        doc_date = None
        if result.document_date:
            try:
                doc_date = date.fromisoformat(result.document_date)
            except ValueError:
                pass

        exp_date = None
        if result.expiration_date:
            try:
                exp_date = date.fromisoformat(result.expiration_date)
            except ValueError:
                pass

        # Determine if AI should auto-apply domain/category
        apply_domain = existing_domain
        apply_category = existing_category
        if not existing_domain and result.domain and result.confidence >= 0.7:
            apply_domain = result.domain
        if not existing_category and result.category and result.confidence >= 0.7:
            apply_category = result.category

        review = "none"
        if result.confidence < 0.7:
            review = "needs_review"

        # ── Subject auto-linking ──────────────────────────────────────
        resolved_subject_id = row["subject_id"]  # keep user-assigned if present
        if not resolved_subject_id:
            resolved_subject_id = await _resolve_subject(
                pool, result.subject_hint, apply_domain
            )

        # Update title if AI generated one and current title looks like a filename
        apply_title = None
        if result.title and row["title"]:
            current = row["title"]
            if "." in current and current.rsplit(".", 1)[-1].lower() in (
                "pdf", "jpg", "jpeg", "png", "tiff", "tif", "doc", "docx",
                "xls", "xlsx", "txt", "csv", "heic", "webp", "gif", "bmp",
            ):
                apply_title = result.title

        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE documents SET
                    title = COALESCE($15, title),
                    ai_summary = $2,
                    ai_extracted_data = $3,
                    ai_action_items = $4,
                    ai_status = 'complete',
                    ai_confidence = $5,
                    review_status = $6,
                    document_date = $7,
                    expiration_date = $8,
                    ai_analyzed_at = $9,
                    ai_prompt_version = $10,
                    domain = COALESCE($11, domain),
                    category = COALESCE($12, category),
                    tags = CASE WHEN array_length(tags, 1) IS NULL THEN $13 ELSE tags || $13 END,
                    subject_id = COALESCE($14, subject_id)
                WHERE id = $1
            """,
                uuid.UUID(doc_id),
                result.summary,
                result.extracted_data,
                result.action_items_raw,
                result.confidence,
                review,
                doc_date,
                exp_date,
                datetime.now(timezone.utc),
                result.prompt_version,
                apply_domain,
                apply_category,
                result.tags or [],
                resolved_subject_id,
                apply_title,
            )

            # Create action items in the action_items table
            if result.action_items:
                for item in result.action_items:
                    due = None
                    if item.get("due_date"):
                        try:
                            due = date.fromisoformat(item["due_date"])
                        except ValueError:
                            pass
                    await conn.execute("""
                        INSERT INTO action_items
                            (domain, subject_id, title, description, due_date,
                             source_type, source_document_id, priority)
                        VALUES ($1, $2, $3, $4, $5, 'ai_extracted', $6, $7)
                    """,
                        apply_domain,
                        resolved_subject_id,
                        item.get("title", "Untitled action"),
                        item.get("description"),
                        due,
                        uuid.UUID(doc_id),
                        item.get("priority", "medium"),
                    )

            # ── Time-series metrics insertion ─────────────────────────
            if result.metrics and resolved_subject_id:
                for m in result.metrics:
                    recorded_at = None
                    if m.get("recorded_at"):
                        try:
                            recorded_at = datetime.fromisoformat(m["recorded_at"]).replace(
                                tzinfo=timezone.utc
                            )
                        except ValueError:
                            pass
                    if not recorded_at and doc_date:
                        recorded_at = datetime.combine(
                            doc_date, datetime.min.time(), tzinfo=timezone.utc
                        )
                    if not recorded_at:
                        recorded_at = datetime.now(timezone.utc)

                    await conn.execute("""
                        INSERT INTO time_series_metrics
                            (subject_id, metric_type, value_numeric, value_text,
                             recorded_at, source, source_document_id, notes)
                        VALUES ($1, $2, $3, $4, $5, 'document_extract', $6, $7)
                    """,
                        resolved_subject_id,
                        m["metric_type"],
                        m.get("value"),
                        m.get("value_text"),
                        recorded_at,
                        uuid.UUID(doc_id),
                        row["title"],
                    )

        logger.info(f"AI analysis complete for {doc_id}: confidence={result.confidence}")

    except Exception as e:
        logger.error(f"AI analysis failed for {doc_id}: {e}")
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE documents SET ai_status = 'failed' WHERE id = $1",
                    uuid.UUID(doc_id),
                )
        except Exception:
            pass


# ── Shared Ingestion Pipeline ────────────────────────────────────────────

async def ingest_file(
    file_path: str,
    *,
    original_filename: str | None = None,
    title: str | None = None,
    domain: str | None = None,
    category: str | None = None,
    subject_id: str | None = None,
    tags: list[str] | None = None,
    source: str = "upload",
    uploaded_by: str | None = None,
) -> dict:
    """Ingest a file into LifeOS: store, extract text, embed, analyze.

    The file at file_path is copied into the storage directory.
    Returns a dict with document metadata.
    """
    settings = get_settings()
    pool = get_pool()

    source_path = Path(file_path)
    if not source_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    doc_id = uuid.uuid4()
    doc_id_str = str(doc_id)
    prefix = doc_id_str[:2]
    ext = source_path.suffix.lower()
    file_dir = os.path.join(settings.upload_dir, "files", prefix)
    os.makedirs(file_dir, exist_ok=True)
    dest_path = os.path.join(file_dir, f"{doc_id_str}{ext}")

    # Copy file to storage
    async with aiofiles.open(file_path, 'rb') as src:
        content = await src.read()
    async with aiofiles.open(dest_path, 'wb') as dst:
        await dst.write(content)

    file_size = len(content)
    mime_type = magic.from_buffer(content[:8192], mime=True)

    from helpers import file_type_from_mime
    f_type = file_type_from_mime(mime_type)

    result = extract_text_with_metadata(dest_path, mime_type)
    content_text = result["text"]
    ocr_applied = result["ocr_applied"]
    ocr_confidence = result["ocr_confidence"]
    page_count = result["page_count"]

    chunks = chunk_text(content_text, settings.chunk_size, settings.chunk_overlap)
    tag_list = tags or []
    doc_title = title or original_filename or source_path.name

    sid = uuid.UUID(subject_id) if subject_id else None

    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO documents (
                id, title, file_path, original_filename, file_size_bytes,
                mime_type, file_type, domain, category, subject_id, source,
                content_text, text_length, ocr_applied, ocr_confidence, page_count,
                embedding_status, ai_status, tags, uploaded_by
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                $12, $13, $14, $15, $16, $17, $18, $19, $20
            )
        """, doc_id, doc_title, dest_path, original_filename or source_path.name,
            file_size, mime_type, f_type, domain, category, sid, source,
            content_text, len(content_text) if content_text else 0,
            ocr_applied, ocr_confidence, page_count,
            "pending", "pending", tag_list, uploaded_by)

        for chunk in chunks:
            await conn.execute("""
                INSERT INTO document_chunks (document_id, chunk_index, chunk_text, char_start, char_end)
                VALUES ($1, $2, $3, $4, $5)
            """, doc_id, chunk["chunk_index"], chunk["text"],
                chunk["char_start"], chunk["char_end"])

    # Index in Qdrant
    embedding_status = "complete"
    try:
        point_ids = index_chunks(doc_id_str, chunks, domain=domain)
        async with pool.acquire() as conn:
            for chunk, point_id in zip(chunks, point_ids):
                await conn.execute("""
                    UPDATE document_chunks SET embedding_id = $1
                    WHERE document_id = $2 AND chunk_index = $3
                """, point_id, doc_id, chunk["chunk_index"])
    except Exception as e:
        logger.error(f"Embedding failed for {doc_id_str}: {e}")
        embedding_status = "failed"

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET embedding_status = $1, ai_status = 'analyzing' WHERE id = $2",
            embedding_status, doc_id,
        )

    # Launch AI analysis in background
    asyncio.create_task(run_ai_analysis(doc_id_str, domain, category))

    return {
        "id": doc_id_str,
        "title": doc_title,
        "file_type": f_type,
        "mime_type": mime_type,
        "file_size_bytes": file_size,
        "text_length": len(content_text) if content_text else 0,
        "chunks": len(chunks),
        "ocr_applied": ocr_applied,
        "embedding_status": embedding_status,
        "ai_status": "analyzing",
    }
