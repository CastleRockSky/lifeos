"""
main.py - LifeOS API Server

FastAPI application providing:
  - Document upload and processing
  - Subject management
  - Hybrid search (semantic + full-text)
  - Agent API auth stub
"""

import hashlib
import os
import uuid
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager

import aiofiles
from fastapi import FastAPI, UploadFile, File, Form, Query, HTTPException, Header, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import asyncpg
import magic
import json as json_mod

from config import get_settings
from processor import extract_text_with_metadata, chunk_text
from search import ensure_collection, index_chunks, hybrid_search, semantic_search, fulltext_search, delete_document_vectors

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ── Domain / Category Constants ──────────────────────────────────────────

DOMAINS = ["medical", "financial", "auto", "home", "vet", "legal", "insurance"]

CATEGORIES = {
    "medical": [
        "lab_result", "visit_note", "prescription", "referral", "imaging_report",
        "surgical_report", "discharge_summary", "vaccination_record", "insurance_eob",
        "insurance_claim", "dental_record", "vision_record", "therapy_note",
        "medical_bill", "prior_authorization", "health_summary", "advance_directive",
    ],
    "financial": [
        "tax_return", "w2", "1099", "bank_statement", "credit_card_statement",
        "loan_agreement", "mortgage_statement", "investment_statement", "receipt",
        "invoice", "pay_stub", "financial_plan", "budget", "credit_report", "tax_estimate",
    ],
    "auto": [
        "registration", "title", "insurance_card", "service_receipt", "recall_notice",
        "purchase_agreement", "lease_agreement", "inspection_report", "warranty", "owners_manual",
    ],
    "home": [
        "mortgage_agreement", "lease", "hoa_document", "insurance_policy", "warranty",
        "contractor_invoice", "permit", "inspection_report", "appraisal",
        "property_tax", "utility_bill", "home_improvement_receipt",
    ],
    "vet": [
        "vet_visit_note", "vaccination_record", "prescription", "lab_result",
        "surgical_report", "boarding_record", "pet_insurance_claim", "adoption_paper",
        "registration", "microchip_record", "dental_record",
    ],
    "legal": [
        "passport", "drivers_license", "birth_certificate", "marriage_certificate",
        "social_security_card", "will", "trust_document", "power_of_attorney",
        "court_document", "contract", "notarized_document",
    ],
    "insurance": [
        "policy_declaration", "premium_notice", "claim", "eob", "coverage_summary",
        "renewal_notice", "cancellation_notice", "agent_correspondence",
    ],
}

ALL_CATEGORIES = [cat for cats in CATEGORIES.values() for cat in cats]

# ── Database Connection Pool ─────────────────────────────────────────────

db_pool: asyncpg.Pool = None


# ── Pydantic Models ──────────────────────────────────────────────────────

class SubjectCreate(BaseModel):
    name: str
    type: str = "person"
    profile_data: dict = {}
    is_primary: bool = False

class SubjectUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    profile_data: Optional[dict] = None

class DocumentUpdate(BaseModel):
    title: Optional[str] = None
    domain: Optional[str] = None
    category: Optional[str] = None
    subject_id: Optional[str] = None
    tags: Optional[list[str]] = None


# ── Helpers ──────────────────────────────────────────────────────────────

def get_user_email(request: Request) -> str:
    return request.headers.get("cf-access-authenticated-user-email", "local")


async def audit_log(action: str, user_email: str = "local", table_name: str = None,
                    record_id: str = None, details: dict = None):
    if not db_pool:
        return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO audit_log (action, user_email, table_name, record_id, details)
                VALUES ($1, $2, $3, $4, $5)
            """, action, user_email, table_name,
                uuid.UUID(record_id) if record_id else None,
                details)
    except Exception as e:
        logger.warning(f"Audit log failed: {e}")


def file_type_from_mime(mime_type: str) -> str:
    if mime_type == "application/pdf":
        return "pdf"
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("text/") or mime_type == "application/json":
        return "text"
    return "other"


# ── Agent Auth Stub ──────────────────────────────────────────────────────

async def validate_agent_key(agent_key: str) -> dict:
    """Look up agent API key and return agent info with allowed domains."""
    if not db_pool or not agent_key:
        raise HTTPException(401, "Invalid API key")

    key_hash = hashlib.sha256(agent_key.encode()).hexdigest()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id, agent_name, allowed_domains
            FROM agent_api_keys
            WHERE key_hash = $1 AND is_active = true AND deleted_at IS NULL
        """, key_hash)

    if not row:
        raise HTTPException(401, "Invalid API key")

    # Update last_used_at
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_api_keys SET last_used_at = NOW() WHERE id = $1",
            row["id"]
        )

    return {
        "id": str(row["id"]),
        "agent_name": row["agent_name"],
        "allowed_domains": list(row["allowed_domains"] or []),
    }


def require_domain(domain: str):
    """Dependency factory to enforce domain scoping for agent keys."""
    async def dependency(x_agent_key: str = Header(alias="X-Agent-Key")):
        agent = await validate_agent_key(x_agent_key)
        if domain not in agent["allowed_domains"]:
            raise HTTPException(403, "Agent not authorized for this domain")
        return agent
    return dependency


# ── App Lifecycle ────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    settings = get_settings()

    async def init_conn(conn):
        await conn.set_type_codec('jsonb', encoder=json_mod.dumps, decoder=json_mod.loads, schema='pg_catalog')
        await conn.set_type_codec('json', encoder=json_mod.dumps, decoder=json_mod.loads, schema='pg_catalog')

    db_pool = await asyncpg.create_pool(settings.database_url, min_size=2, max_size=10, init=init_conn)
    logger.info("Database pool created")

    ensure_collection()

    os.makedirs(settings.upload_dir, exist_ok=True)
    os.makedirs(os.path.join(settings.upload_dir, "files"), exist_ok=True)

    yield

    await db_pool.close()
    logger.info("Database pool closed")


app = FastAPI(title="LifeOS API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Health & Stats ───────────────────────────────────────────────────────

@app.get("/api/health")
async def health_check():
    db_ok = False
    qdrant_ok = False

    try:
        async with db_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_ok = True
    except Exception:
        pass

    try:
        from qdrant_client import QdrantClient
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


@app.get("/api/stats")
async def get_stats():
    async with db_pool.acquire() as conn:
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

    return {
        "data": {
            "documents": total_docs,
            "storage_bytes": total_size,
            "chunks": total_chunks,
            "subjects": total_subjects,
            "by_domain": {r["domain"]: r["count"] for r in domain_rows},
        }
    }


# ── Subjects CRUD ────────────────────────────────────────────────────────

@app.get("/api/subjects")
async def list_subjects():
    async with db_pool.acquire() as conn:
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


@app.post("/api/subjects")
async def create_subject(body: SubjectCreate, request: Request):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO subjects (name, type, profile_data, is_primary)
            VALUES ($1, $2, $3, $4)
            RETURNING *
        """, body.name, body.type, body.profile_data, body.is_primary)

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


@app.get("/api/subjects/{subject_id}")
async def get_subject(subject_id: str):
    sid = uuid.UUID(subject_id)
    async with db_pool.acquire() as conn:
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


@app.patch("/api/subjects/{subject_id}")
async def update_subject(subject_id: str, body: SubjectUpdate, request: Request):
    sid = uuid.UUID(subject_id)
    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT * FROM subjects WHERE id = $1 AND deleted_at IS NULL", sid
        )
        if not existing:
            raise HTTPException(404, "Subject not found")

        await conn.execute("""
            UPDATE subjects SET
                name = COALESCE($2, name),
                type = COALESCE($3, type),
                profile_data = COALESCE($4, profile_data)
            WHERE id = $1
        """, sid, body.name, body.type, body.profile_data)

    await audit_log("update", get_user_email(request), "subjects", subject_id)
    return {"data": {"id": subject_id, "updated": True}}


# ── Document Upload & Processing ─────────────────────────────────────────

@app.post("/api/documents/upload")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(None),
    domain: str = Form(None),
    category: str = Form(None),
    subject_id: str = Form(None),
    tags: str = Form(""),
):
    settings = get_settings()
    user_email = get_user_email(request)

    # Validate domain/category
    if domain and domain not in DOMAINS:
        raise HTTPException(400, f"Invalid domain: {domain}")
    if category and category not in ALL_CATEGORIES:
        raise HTTPException(400, f"Invalid category: {category}")

    # Generate file path
    doc_id = uuid.uuid4()
    doc_id_str = str(doc_id)
    prefix = doc_id_str[:2]
    ext = Path(file.filename).suffix.lower() if file.filename else ""
    file_dir = os.path.join(settings.upload_dir, "files", prefix)
    os.makedirs(file_dir, exist_ok=True)
    file_path = os.path.join(file_dir, f"{doc_id_str}{ext}")

    # Save file
    content = await file.read()
    async with aiofiles.open(file_path, 'wb') as f:
        await f.write(content)

    file_size = len(content)

    # Detect MIME type
    mime_type = magic.from_buffer(content[:8192], mime=True)
    f_type = file_type_from_mime(mime_type)

    # Extract text
    result = extract_text_with_metadata(file_path, mime_type)
    content_text = result["text"]
    ocr_applied = result["ocr_applied"]
    ocr_confidence = result["ocr_confidence"]
    page_count = result["page_count"]

    # Chunk text
    chunks = chunk_text(content_text, settings.chunk_size, settings.chunk_overlap)

    # Parse tags
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    # Use filename as title if not provided
    doc_title = title or file.filename or "Untitled"

    # Validate subject
    sid = None
    if subject_id:
        sid = uuid.UUID(subject_id)

    # Insert document
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO documents (
                id, title, file_path, original_filename, file_size_bytes,
                mime_type, file_type, domain, category, subject_id, source,
                content_text, text_length, ocr_applied, ocr_confidence, page_count,
                embedding_status, tags, uploaded_by
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                $12, $13, $14, $15, $16, $17, $18, $19
            )
        """, doc_id, doc_title, file_path, file.filename, file_size,
            mime_type, f_type, domain, category, sid, "upload",
            content_text, len(content_text) if content_text else 0,
            ocr_applied, ocr_confidence, page_count,
            "pending", tag_list, user_email)

        # Insert chunks
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
        # Update chunk embedding IDs
        async with db_pool.acquire() as conn:
            for chunk, point_id in zip(chunks, point_ids):
                await conn.execute("""
                    UPDATE document_chunks SET embedding_id = $1
                    WHERE document_id = $2 AND chunk_index = $3
                """, point_id, doc_id, chunk["chunk_index"])
    except Exception as e:
        logger.error(f"Embedding failed for {doc_id_str}: {e}")
        embedding_status = "failed"

    # Update embedding status
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET embedding_status = $1 WHERE id = $2",
            embedding_status, doc_id
        )

    await audit_log("upload", user_email, "documents", doc_id_str,
                    {"filename": file.filename, "size": file_size})

    return {
        "data": {
            "id": doc_id_str,
            "title": doc_title,
            "file_type": f_type,
            "mime_type": mime_type,
            "file_size_bytes": file_size,
            "text_length": len(content_text) if content_text else 0,
            "chunks": len(chunks),
            "ocr_applied": ocr_applied,
            "embedding_status": embedding_status,
        }
    }


# ── Document CRUD ────────────────────────────────────────────────────────

@app.get("/api/documents")
async def list_documents(
    domain: str = Query(None),
    category: str = Query(None),
    subject_id: str = Query(None),
    q: str = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    conditions = ["d.deleted_at IS NULL"]
    params = []
    idx = 0

    if domain:
        idx += 1
        conditions.append(f"d.domain = ${idx}")
        params.append(domain)
    if category:
        idx += 1
        conditions.append(f"d.category = ${idx}")
        params.append(category)
    if subject_id:
        idx += 1
        conditions.append(f"d.subject_id = ${idx}")
        params.append(uuid.UUID(subject_id))
    if q:
        idx += 1
        conditions.append(f"""
            to_tsvector('english', COALESCE(d.title, '') || ' ' || COALESCE(d.content_text, ''))
            @@ plainto_tsquery('english', ${idx})
        """)
        params.append(q)

    where = " AND ".join(conditions)
    offset = (page - 1) * per_page

    async with db_pool.acquire() as conn:
        count_params = list(params)
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM documents d WHERE {where}", *count_params
        )

        idx += 1
        params.append(per_page)
        limit_idx = idx
        idx += 1
        params.append(offset)
        offset_idx = idx

        rows = await conn.fetch(f"""
            SELECT d.id, d.title, d.domain, d.category, d.file_type, d.mime_type,
                   d.file_size_bytes, d.text_length, d.ocr_applied, d.embedding_status,
                   d.tags, d.ingested_at, d.original_filename, d.subject_id,
                   s.name as subject_name
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
                "tags": list(r["tags"]) if r["tags"] else [],
                "original_filename": r["original_filename"],
                "subject_id": str(r["subject_id"]) if r["subject_id"] else None,
                "subject_name": r["subject_name"],
                "ingested_at": r["ingested_at"].isoformat() if r["ingested_at"] else None,
            }
            for r in rows
        ],
        "meta": {
            "total": total,
            "page": page,
            "per_page": per_page,
        },
    }


@app.get("/api/documents/{document_id}")
async def get_document(document_id: str):
    did = uuid.UUID(document_id)
    async with db_pool.acquire() as conn:
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
        }
    }


@app.get("/api/documents/{document_id}/file")
async def download_document(document_id: str, request: Request):
    did = uuid.UUID(document_id)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT file_path, original_filename, mime_type FROM documents WHERE id = $1 AND deleted_at IS NULL",
            did
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
    )


@app.patch("/api/documents/{document_id}")
async def update_document(document_id: str, body: DocumentUpdate, request: Request):
    did = uuid.UUID(document_id)

    if body.domain and body.domain not in DOMAINS:
        raise HTTPException(400, f"Invalid domain: {body.domain}")
    if body.category and body.category not in ALL_CATEGORIES:
        raise HTTPException(400, f"Invalid category: {body.category}")

    async with db_pool.acquire() as conn:
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
                tags = COALESCE($6, tags)
            WHERE id = $1
        """, did, body.title, body.domain, body.category, sid, body.tags)

    await audit_log("update", get_user_email(request), "documents", document_id)
    return {"data": {"id": document_id, "updated": True}}


@app.delete("/api/documents/{document_id}")
async def delete_document(document_id: str, request: Request):
    did = uuid.UUID(document_id)
    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id FROM documents WHERE id = $1 AND deleted_at IS NULL", did
        )
        if not existing:
            raise HTTPException(404, "Document not found")

        await conn.execute(
            "UPDATE documents SET deleted_at = NOW() WHERE id = $1", did
        )

    # Clean up Qdrant vectors
    try:
        delete_document_vectors(document_id)
    except Exception as e:
        logger.warning(f"Failed to delete vectors for {document_id}: {e}")

    await audit_log("delete", get_user_email(request), "documents", document_id)
    return {"data": {"id": document_id, "deleted": True}}


# ── Search ───────────────────────────────────────────────────────────────

@app.get("/api/search")
async def search_documents(
    q: str = Query(..., min_length=1),
    domain: str = Query(None),
    limit: int = Query(10, ge=1, le=50),
):
    """Hybrid search (semantic + full-text with RRF)."""
    results = await hybrid_search(
        query=q,
        limit=limit,
        domain=domain,
        db_pool=db_pool,
    )

    # Enrich with document metadata
    if results:
        doc_ids = list(set(r["document_id"] for r in results))
        async with db_pool.acquire() as conn:
            doc_rows = await conn.fetch("""
                SELECT id, title, domain, category, ingested_at, original_filename
                FROM documents WHERE id = ANY($1::uuid[])
            """, [uuid.UUID(d) for d in doc_ids])
            doc_map = {str(r["id"]): r for r in doc_rows}

        for r in results:
            doc = doc_map.get(r["document_id"], {})
            r["title"] = doc.get("title") if doc else None
            r["document_domain"] = doc.get("domain") if doc else None
            r["category"] = doc.get("category") if doc else None
            r["original_filename"] = doc.get("original_filename") if doc else None
            r["ingested_at"] = doc["ingested_at"].isoformat() if doc and doc.get("ingested_at") else None

    await audit_log("search", details={"query": q, "domain": domain, "results": len(results)})
    return {"data": results}


@app.get("/api/search/text")
async def search_text_only(
    q: str = Query(..., min_length=1),
    domain: str = Query(None),
    limit: int = Query(10, ge=1, le=50),
):
    """Full-text search only (PostgreSQL tsvector)."""
    results = await fulltext_search(
        query=q,
        limit=limit,
        domain=domain,
        db_pool=db_pool,
    )

    return {"data": results}


# ── Domain/Category Reference ────────────────────────────────────────────

@app.get("/api/domains")
async def list_domains():
    return {"data": DOMAINS}


@app.get("/api/categories")
async def list_categories(domain: str = Query(None)):
    if domain:
        if domain not in CATEGORIES:
            raise HTTPException(400, f"Invalid domain: {domain}")
        return {"data": CATEGORIES[domain]}
    return {"data": CATEGORIES}
