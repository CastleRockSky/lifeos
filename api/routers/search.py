"""
routers/search.py — Hybrid search, text search, Q&A, expirations.
"""

import uuid
import logging
from datetime import date, timedelta

from fastapi import APIRouter, Query, HTTPException, Request

from config import get_settings
from database import get_pool
from helpers import audit_log, get_user_email
from models import AskRequest
from rate_limit import take as rate_limit_take
from search import hybrid_search, fulltext_search, semantic_search

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["search"])


@router.get("/search")
async def search_documents(
    q: str = Query(..., min_length=1),
    domain: str = Query(None),
    limit: int = Query(10, ge=1, le=50),
):
    """Hybrid search (semantic + full-text with RRF)."""
    pool = get_pool()
    results = await hybrid_search(
        query=q, limit=limit, domain=domain, db_pool=pool,
    )

    if results:
        doc_ids = list(set(r["document_id"] for r in results))
        async with pool.acquire() as conn:
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


@router.get("/search/text")
async def search_text_only(
    q: str = Query(..., min_length=1),
    domain: str = Query(None),
    limit: int = Query(10, ge=1, le=50),
):
    """Full-text search only (PostgreSQL tsvector)."""
    pool = get_pool()
    results = await fulltext_search(
        query=q, limit=limit, domain=domain, db_pool=pool,
    )
    return {"data": results}


@router.post("/ask")
async def ask_question(body: AskRequest, request: Request):
    """RAG-powered Q&A: embed question, search for context, call Claude."""
    settings = get_settings()
    pool = get_pool()

    if not settings.anthropic_api_key:
        raise HTTPException(503, "AI not configured")

    # Per-user rate limit to prevent runaway Claude API costs.
    user_key = get_user_email(request) or "anon"
    if not rate_limit_take(f"ask:{user_key}", settings.qa_rate_limit_per_minute):
        raise HTTPException(
            429,
            f"Rate limit exceeded ({settings.qa_rate_limit_per_minute}/min). Try again shortly.",
        )

    # Get relevant chunks via semantic search
    chunks = semantic_search(
        query=body.question, limit=10, domain=body.domain, score_threshold=0.25,
    )

    if not chunks:
        return {
            "data": {
                "answer": "I couldn't find any relevant documents to answer your question. Try uploading more documents or rephrasing your question.",
                "sources": [],
            }
        }

    # Build context from chunks (limit to ~4000 tokens ≈ ~16000 chars)
    context_parts = []
    char_count = 0
    doc_ids_seen = set()
    for chunk in chunks:
        text = chunk.get("chunk_text", "")
        if char_count + len(text) > 16000:
            break
        context_parts.append(text)
        char_count += len(text)
        doc_ids_seen.add(chunk["document_id"])

    context = "\n---\n".join(context_parts)

    # Get document metadata for sources
    sources = []
    if doc_ids_seen:
        async with pool.acquire() as conn:
            source_rows = await conn.fetch("""
                SELECT id, title, domain, category
                FROM documents WHERE id = ANY($1::uuid[]) AND deleted_at IS NULL
            """, [uuid.UUID(d) for d in doc_ids_seen])

        source_map = {str(r["id"]): r for r in source_rows}
        for chunk in chunks:
            did = chunk["document_id"]
            if did in source_map and did not in [s["document_id"] for s in sources]:
                doc = source_map[did]
                sources.append({
                    "document_id": did,
                    "title": doc["title"],
                    "domain": doc["domain"],
                    "relevance": round(chunk.get("score", 0), 3),
                })

    # Call Claude for answer
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    system_prompt = """You are LifeOS, a personal document assistant. Answer questions based on the provided document context.

Rules:
- Only answer based on the provided context. If the context doesn't contain enough information, say so.
- Be concise and direct.
- When citing information, mention which document it came from if you can identify it.
- If the question is about dates, deadlines, or amounts, be precise."""

    user_message = f"""Context from my documents:
{context}

Question: {body.question}"""

    try:
        message = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            temperature=0.1,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        answer = message.content[0].text.strip()
    except Exception as e:
        logger.error(f"Q&A Claude call failed: {e}")
        raise HTTPException(502, "AI service unavailable")

    await audit_log("ask", details={"question": body.question, "sources": len(sources)})

    return {
        "data": {
            "answer": answer,
            "sources": sources,
        }
    }


@router.get("/expirations")
async def list_expirations(
    domain: str = Query(None),
    days: int = Query(90, ge=1, le=365),
):
    """Documents with upcoming expiration dates."""
    pool = get_pool()
    cutoff = date.today() + timedelta(days=days)

    conditions = [
        "d.deleted_at IS NULL",
        "d.expiration_date IS NOT NULL",
        "d.expiration_date <= $1",
    ]
    params = [cutoff]
    idx = 1

    if domain:
        idx += 1
        conditions.append(f"d.domain = ${idx}")
        params.append(domain)

    where = " AND ".join(conditions)

    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT d.id, d.title, d.domain, d.category, d.expiration_date,
                   d.ai_summary, d.original_filename
            FROM documents d
            WHERE {where}
            ORDER BY d.expiration_date ASC
        """, *params)

    return {
        "data": [
            {
                "id": str(r["id"]),
                "title": r["title"],
                "domain": r["domain"],
                "category": r["category"],
                "expiration_date": r["expiration_date"].isoformat(),
                "ai_summary": r["ai_summary"],
                "original_filename": r["original_filename"],
                "is_expired": r["expiration_date"] < date.today(),
                "days_until": (r["expiration_date"] - date.today()).days,
            }
            for r in rows
        ],
    }
