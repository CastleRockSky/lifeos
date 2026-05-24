"""
ingest.py — Shared document ingestion pipeline.

Used by both the upload endpoint and the inbox watcher.
"""

import asyncio
import hashlib
import json
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

_PRIMARY_FALLBACK_DOMAINS = {"medical", "financial", "tax", "legal", "insurance"}


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

async def run_ai_analysis(
    doc_id: str,
    domain: str = None,
    category: str = None,
    stage_metadata: bool = False,
):
    """Background task: run AI analysis on a document and update DB.

    When stage_metadata is True (re-analysis of an existing document), the AI's
    proposed metadata — title, domain, category, dates, summary, tags — is NOT
    applied directly. It is stored in documents.ai_suggestion for the user to
    review and save in the UI. Extracted action items, structured records and
    metrics still apply automatically either way.
    """
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT d.content_text, d.title, d.mime_type, d.domain, d.category,
                       d.subject_id, d.email_message_id,
                       em.sender AS email_sender,
                       em.original_sender AS email_original_sender,
                       em.subject AS email_subject,
                       em.clean_subject AS email_clean_subject
                FROM documents d
                LEFT JOIN email_messages em ON em.id = d.email_message_id
                WHERE d.id = $1
            """, uuid.UUID(doc_id))
        if not row or not row["content_text"]:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE documents SET ai_status = 'skipped' WHERE id = $1",
                    uuid.UUID(doc_id),
                )
            # A text-less scan can still be an exact-hash duplicate.
            from dedup import flag_duplicates_for_document
            await flag_duplicates_for_document(doc_id)
            return

        # Import here to avoid circular imports at module load
        from ai_analyzer import analyze_document

        existing_domain = domain or row["domain"]
        existing_category = category or row["category"]

        # If this doc came from a forwarded email, surface the email
        # subject/sender to the AI as additional classification context.
        extra_context = ""
        if row["email_message_id"]:
            ctx_lines = []
            sender = row["email_original_sender"] or row["email_sender"]
            if sender:
                ctx_lines.append(f"Forwarded email from: {sender}")
            subj = row["email_clean_subject"] or row["email_subject"]
            if subj:
                ctx_lines.append(f"Email subject: {subj}")
            extra_context = "\n".join(ctx_lines)

        result = await analyze_document(
            text=row["content_text"],
            filename=row["title"],
            mime_type=row["mime_type"],
            existing_domain=existing_domain,
            existing_category=existing_category,
            extra_context=extra_context,
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
            if stage_metadata:
                # Re-analysis: stage the proposed metadata for review instead of
                # applying it. Extracted data, action items, records and metrics
                # below still apply normally.
                suggestion = {
                    "title": result.title,
                    "summary": result.summary,
                    "domain": result.domain,
                    "category": result.category,
                    "document_date": result.document_date,
                    "expiration_date": result.expiration_date,
                    "tags": result.tags or [],
                    "confidence": result.confidence,
                    "analyzed_at": datetime.now(timezone.utc).isoformat(),
                }
                await conn.execute("""
                    UPDATE documents SET
                        ai_extracted_data = $2,
                        ai_action_items = $3,
                        ai_status = 'complete',
                        ai_confidence = $4,
                        ai_analyzed_at = $5,
                        ai_prompt_version = $6,
                        subject_id = COALESCE($7, subject_id),
                        ai_suggestion = $8
                    WHERE id = $1
                """,
                    uuid.UUID(doc_id),
                    result.extracted_data,
                    result.action_items_raw,
                    result.confidence,
                    datetime.now(timezone.utc),
                    result.prompt_version,
                    resolved_subject_id,
                    suggestion,
                )
            else:
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

            # ── Structured records insertion ──────────────────────────
            # AI-extracted records get inserted with this document as the source.
            # For records where we can identify an "identity field" (medication.name,
            # provider.name, condition.name) we upsert against an existing row for
            # the same subject so re-uploading a refill doesn't duplicate the med.
            if result.structured_records:
                from datetime import date as _date
                for sr in result.structured_records:
                    rtype = sr["record_type"]
                    data = sr["data"] or {}
                    identity_key = None
                    if rtype in (
                        "medication", "provider", "condition",
                        "pet_medication", "vet_provider", "pet_condition",
                    ):
                        identity_key = (data.get("name") or "").strip().lower() or None

                    record_id = None
                    if identity_key and resolved_subject_id:
                        existing = await conn.fetchrow("""
                            SELECT id, data FROM structured_records
                            WHERE record_type = $1
                              AND subject_id = $2
                              AND deleted_at IS NULL
                              AND LOWER(COALESCE(data->>'name', '')) = $3
                            ORDER BY updated_at DESC
                            LIMIT 1
                        """, rtype, resolved_subject_id, identity_key)
                        if existing:
                            merged = dict(existing["data"]) if isinstance(existing["data"], dict) else {}
                            merged.update({k: v for k, v in data.items() if v is not None})
                            await conn.execute("""
                                UPDATE structured_records
                                SET data = $1::jsonb,
                                    source_document_id = $2,
                                    domain = COALESCE(domain, $3)
                                WHERE id = $4
                            """, merged,
                                uuid.UUID(doc_id), apply_domain, existing["id"])
                            record_id = existing["id"]

                    if record_id is None:
                        new_row = await conn.fetchrow("""
                            INSERT INTO structured_records
                                (record_type, domain, subject_id, data, source_document_id)
                            VALUES ($1, $2, $3, $4::jsonb, $5)
                            RETURNING id
                        """, rtype, apply_domain, resolved_subject_id,
                            data, uuid.UUID(doc_id))
                        record_id = new_row["id"]

                    # Recurring action item for known record types
                    # (financial obligations, home/auto maintenance, registration renewals,
                    # pet vaccinations and preventatives).
                    if rtype in (
                        "credit_account", "loan", "recurring_expense",
                        "appliance", "home_maintenance_schedule",
                        "maintenance_schedule", "vehicle",
                        "pet_vaccination", "preventative_schedule", "pet_medication",
                        "insurance_policy", "identity_document",
                        "tax_item",
                    ):
                        try:
                            from recurrences import ensure_recurring_action_item
                            await ensure_recurring_action_item(
                                conn,
                                record_type=rtype,
                                record_id=record_id,
                                data=data,
                                subject_id=resolved_subject_id,
                                source_document_id=uuid.UUID(doc_id),
                            )
                        except Exception as rec_err:
                            logger.warning(f"Recurring action item failed for {doc_id}: {rec_err}")

                    # Refill action item for medications.
                    if rtype == "medication":
                        refill = data.get("refill_date")
                        if isinstance(refill, str) and refill:
                            try:
                                refill_dt = _date.fromisoformat(refill)
                            except ValueError:
                                refill_dt = None
                        else:
                            refill_dt = None
                        if refill_dt:
                            med_name = data.get("name") or "medication"
                            # Avoid stacking duplicate refill action items for the
                            # same refill date.
                            await conn.execute("""
                                INSERT INTO action_items
                                    (domain, subject_id, title, description, due_date,
                                     source_type, source_document_id, source_record_id, priority)
                                SELECT 'medical', $1, $2, $3, $4, 'ai_extracted', $5, $6, 'medium'
                                WHERE NOT EXISTS (
                                    SELECT 1 FROM action_items
                                    WHERE source_record_id = $6
                                      AND due_date = $4
                                      AND deleted_at IS NULL
                                )
                            """,
                                resolved_subject_id,
                                f"Refill {med_name}",
                                f"Refill due based on {row['title']}",
                                refill_dt,
                                uuid.UUID(doc_id),
                                record_id,
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

        # ── Email sender learning ─────────────────────────────────────
        # If this doc came from a forwarded email and the AI is confident,
        # update the email_sender_map so future emails from the same sender
        # can be pre-classified.
        if result.confidence >= 0.75 and apply_domain:
            try:
                async with pool.acquire() as conn:
                    email_row = await conn.fetchrow("""
                        SELECT em.sender, em.original_sender
                        FROM documents d
                        JOIN email_messages em ON em.id = d.email_message_id
                        WHERE d.id = $1
                    """, uuid.UUID(doc_id))
                if email_row:
                    from email_ingest import learn_sender_mapping
                    sender_to_learn = email_row["original_sender"] or email_row["sender"]
                    if sender_to_learn:
                        await learn_sender_mapping(
                            sender_to_learn,
                            domain=apply_domain,
                            category=apply_category,
                            subject_hint=result.subject_hint,
                        )
            except Exception as learn_err:
                logger.warning(f"Sender mapping update failed for {doc_id}: {learn_err}")

        # ── Vehicle linking (auto domain only, Phase 6) ───────────────
        # If we just identified an auto doc with VIN or year+make+model in
        # the extraction, attach it to the matching vehicle so it shows up
        # on the per-vehicle Documents panel. Never raises.
        if apply_domain == "auto" and result.extracted_data:
            try:
                from auto_linking import match_document_to_vehicle
                pool = get_pool()
                async with pool.acquire() as conn:
                    veh_rows = await conn.fetch(
                        """SELECT id, data FROM structured_records
                           WHERE record_type = 'vehicle' AND deleted_at IS NULL
                             AND (data->>'status' IS NULL
                                  OR data->>'status' NOT IN
                                     ('merged','archived','sold','totaled'))"""
                    )
                    candidates = [
                        {"id": str(r["id"]),
                         "data": r["data"] if isinstance(r["data"], dict) else json.loads(r["data"])}
                        for r in veh_rows
                    ]
                    match_id = match_document_to_vehicle(result.extracted_data, candidates)
                    if match_id:
                        await conn.execute(
                            """UPDATE documents SET linked_record_id = $1
                               WHERE id = $2 AND linked_record_id IS NULL""",
                            uuid.UUID(match_id), uuid.UUID(doc_id),
                        )
                        logger.info(f"Auto-linked doc {doc_id} to vehicle {match_id}")
            except Exception as link_err:
                logger.warning(f"Vehicle linking failed for {doc_id}: {link_err}")

        # ── Duplicate detection ───────────────────────────────────────
        # Runs here (not at ingest time) so document_date is known and the
        # date-aware filter can apply. Never raises.
        from dedup import flag_duplicates_for_document
        await flag_duplicates_for_document(doc_id)

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

    # Read source first so we can hash and short-circuit exact duplicates
    # before writing a redundant copy to disk.
    async with aiofiles.open(file_path, 'rb') as src:
        content = await src.read()

    file_size = len(content)
    file_hash = hashlib.sha256(content).hexdigest()
    mime_type = magic.from_buffer(content[:8192], mime=True)

    from helpers import file_type_from_mime
    f_type = file_type_from_mime(mime_type)

    # Exact-hash short-circuit: if a non-deleted doc with these bytes already
    # exists, don't ingest it again. Return a "skipped" result pointing to it.
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            """SELECT id, title, mime_type, file_size_bytes, ai_status,
                      embedding_status, text_length, page_count, ocr_applied
               FROM documents
               WHERE file_hash = $1 AND deleted_at IS NULL
               ORDER BY created_at ASC LIMIT 1""",
            file_hash,
        )
    if existing:
        logger.info(
            f"Skipping ingest: file_hash matches existing doc {existing['id']} "
            f"(original_filename={original_filename})"
        )
        return {
            "id": str(existing["id"]),
            "title": existing["title"],
            "file_type": file_type_from_mime(existing["mime_type"] or ""),
            "mime_type": existing["mime_type"],
            "file_size_bytes": existing["file_size_bytes"],
            "text_length": existing["text_length"] or 0,
            "chunks": 0,
            "ocr_applied": bool(existing["ocr_applied"]),
            "embedding_status": existing["embedding_status"],
            "ai_status": existing["ai_status"],
            "skipped": True,
            "skipped_reason": "exact_duplicate",
            "duplicate_of_id": str(existing["id"]),
            "duplicate_of_title": existing["title"],
        }

    doc_id = uuid.uuid4()
    doc_id_str = str(doc_id)
    prefix = doc_id_str[:2]
    ext = source_path.suffix.lower()
    file_dir = os.path.join(settings.upload_dir, "files", prefix)
    os.makedirs(file_dir, exist_ok=True)
    dest_path = os.path.join(file_dir, f"{doc_id_str}{ext}")

    # Copy file to storage (only after we've confirmed it's not a duplicate)
    async with aiofiles.open(dest_path, 'wb') as dst:
        await dst.write(content)

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
                embedding_status, ai_status, tags, uploaded_by, file_hash
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                $12, $13, $14, $15, $16, $17, $18, $19, $20, $21
            )
        """, doc_id, doc_title, dest_path, original_filename or source_path.name,
            file_size, mime_type, f_type, domain, category, sid, source,
            content_text, len(content_text) if content_text else 0,
            ocr_applied, ocr_confidence, page_count,
            "pending", "pending", tag_list, uploaded_by, file_hash)

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
