"""
dedup.py — Document deduplication detection.

Two layers, both of which *flag* duplicates but never auto-reject:
  Layer 1 — exact:    SHA256 of the file bytes (zero false positives).
  Layer 2 — semantic: cosine similarity of a text sample against the
                      existing Qdrant index, with a date-aware filter so
                      recurring documents (monthly statements, EOBs) that
                      look alike but cover different periods don't flag.

Detected duplicates are written to the `duplicate_flags` table for review.
"""

import hashlib
import logging
import uuid
from datetime import date, datetime

import asyncpg

from config import get_settings
from database import get_pool
from search import embed_query, get_qdrant_client

logger = logging.getLogger(__name__)


def compute_file_hash(file_path: str) -> str:
    """SHA256 of a file, read in 64KB chunks."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def date_within_tolerance(
    document_date: date | None,
    match_date: date | None,
    tolerance_days: int,
) -> bool:
    """Decide whether two documents' dates are close enough to be duplicates.

    tolerance_days: -1 disables the check (always True); 0 is strict (dates
    must be identical); N > 0 allows up to an N-day gap. If either date is
    missing the check can't apply, so it falls through to True.
    """
    if tolerance_days < 0:
        return True
    if document_date is None or match_date is None:
        return True
    return abs((document_date - match_date).days) <= tolerance_days


async def check_exact_duplicate(
    file_hash: str,
    pool: asyncpg.Pool,
    exclude_doc_id: str | None = None,
    before: datetime | None = None,
) -> dict | None:
    """Return the oldest non-deleted document sharing this file hash, or None.

    `before` restricts the search to documents created earlier — so the newer
    upload is flagged as the duplicate, never the original.
    """
    if not file_hash:
        return None
    exclude = uuid.UUID(exclude_doc_id) if exclude_doc_id else None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, title, original_filename, document_date, source
               FROM documents
               WHERE file_hash = $1 AND deleted_at IS NULL
                 AND ($2::uuid IS NULL OR id != $2)
                 AND ($3::timestamptz IS NULL OR created_at < $3)
               ORDER BY created_at
               LIMIT 1""",
            file_hash, exclude, before,
        )
    if not row:
        return None
    return {
        "document_id": str(row["id"]),
        "title": row["title"],
        "original_filename": row["original_filename"],
        "document_date": row["document_date"].isoformat() if row["document_date"] else None,
        "source": row["source"],
    }


async def check_semantic_duplicates(
    extracted_text: str,
    pool: asyncpg.Pool,
    exclude_doc_id: str | None = None,
    document_date: date | None = None,
    before: datetime | None = None,
) -> list[dict]:
    """Find near-duplicate documents by semantic similarity.

    Embeds a sample of `extracted_text`, searches Qdrant for similar chunks,
    groups hits by document, then drops matches whose date is inconsistent
    with `document_date` (see date_within_tolerance).
    """
    settings = get_settings()
    if not extracted_text or len(extracted_text.strip()) < 50:
        return []

    sample = extracted_text[:1500]
    try:
        query_vector = embed_query(sample)
        results = get_qdrant_client().query_points(
            collection_name=settings.qdrant_collection,
            query=query_vector,
            limit=20,
            score_threshold=settings.dedup_semantic_threshold,
        )
    except Exception as e:
        logger.warning(f"Dedup semantic search failed: {e}")
        return []

    # Highest score per other document.
    doc_scores: dict[str, float] = {}
    for point in results.points:
        doc_id = (point.payload or {}).get("document_id")
        if not doc_id or doc_id == exclude_doc_id:
            continue
        if doc_id not in doc_scores or point.score > doc_scores[doc_id]:
            doc_scores[doc_id] = point.score
    if not doc_scores:
        return []

    doc_ids = list(doc_scores.keys())[:5]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, title, original_filename, document_date, source
               FROM documents
               WHERE id = ANY($1::uuid[]) AND deleted_at IS NULL
                 AND ($2::timestamptz IS NULL OR created_at < $2)""",
            [uuid.UUID(d) for d in doc_ids], before,
        )

    matches = []
    for row in rows:
        if not date_within_tolerance(
            document_date, row["document_date"], settings.dedup_date_diff_days
        ):
            logger.info(
                f"Dedup: skipping semantic match {row['id']} — date out of tolerance"
            )
            continue
        did = str(row["id"])
        matches.append({
            "document_id": did,
            "title": row["title"],
            "original_filename": row["original_filename"],
            "document_date": row["document_date"].isoformat() if row["document_date"] else None,
            "source": row["source"],
            "similarity_score": doc_scores[did],
        })
    matches.sort(key=lambda m: m["similarity_score"], reverse=True)
    return matches


async def check_duplicates(
    file_path: str,
    extracted_text: str,
    pool: asyncpg.Pool,
    exclude_doc_id: str | None = None,
    document_date: date | None = None,
    file_hash: str | None = None,
    before: datetime | None = None,
) -> dict:
    """Run both layers. `file_hash` is recomputed from `file_path` if not given.

    `before` restricts matches to documents created earlier, so only the newer
    of a pair is flagged as the duplicate.
    """
    if file_hash is None:
        try:
            file_hash = compute_file_hash(file_path)
        except OSError as e:
            logger.warning(f"Dedup hash failed for {file_path}: {e}")
            file_hash = ""

    exact_match = await check_exact_duplicate(file_hash, pool, exclude_doc_id, before)
    semantic_matches = await check_semantic_duplicates(
        extracted_text, pool, exclude_doc_id, document_date=document_date, before=before
    )
    # Don't double-flag the exact match as a semantic match too.
    if exact_match:
        semantic_matches = [
            m for m in semantic_matches if m["document_id"] != exact_match["document_id"]
        ]
    return {
        "file_hash": file_hash,
        "exact_match": exact_match,
        "semantic_matches": semantic_matches,
        "has_duplicates": bool(exact_match or semantic_matches),
    }


async def create_duplicate_flags(document_id: str, dedup_result: dict, pool: asyncpg.Pool) -> int:
    """Persist detected duplicates into `duplicate_flags`. Returns the count written."""
    doc_uuid = uuid.UUID(document_id)
    written = 0
    entries = []
    if dedup_result.get("exact_match"):
        entries.append(("exact", 1.0, dedup_result["exact_match"]["document_id"]))
    for m in dedup_result.get("semantic_matches", []):
        entries.append(("semantic", float(m["similarity_score"]), m["document_id"]))

    async with pool.acquire() as conn:
        for match_type, score, dup_of in entries:
            try:
                result = await conn.execute(
                    """INSERT INTO duplicate_flags
                       (document_id, duplicate_of_id, match_type, similarity_score)
                       VALUES ($1, $2, $3, $4)
                       ON CONFLICT (document_id, duplicate_of_id) DO NOTHING""",
                    doc_uuid, uuid.UUID(dup_of), match_type, score,
                )
                if result.endswith("1"):
                    written += 1
            except Exception as e:
                logger.warning(f"Failed to write duplicate flag for {document_id}: {e}")
    return written


async def flag_duplicates_for_document(doc_id: str) -> int:
    """Load a document and flag any duplicates of it. Returns flags written.

    Safe to call from the ingestion pipeline — never raises; a failure here
    must not break analysis. Honors the `dedup_enabled` setting.
    """
    settings = get_settings()
    if not settings.dedup_enabled:
        return 0
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT file_path, file_hash, content_text, document_date, created_at
                   FROM documents WHERE id = $1 AND deleted_at IS NULL""",
                uuid.UUID(doc_id),
            )
        if not row:
            return 0
        result = await check_duplicates(
            row["file_path"],
            row["content_text"] or "",
            pool,
            exclude_doc_id=doc_id,
            document_date=row["document_date"],
            file_hash=row["file_hash"],
            before=row["created_at"],
        )
        if not result["has_duplicates"]:
            return 0
        n = await create_duplicate_flags(doc_id, result, pool)
        if n:
            logger.info(f"Dedup: flagged {n} possible duplicate(s) for {doc_id}")
        return n
    except Exception as e:
        logger.warning(f"Dedup flagging failed for {doc_id}: {e}")
        return 0
