"""
search.py - Vector search and full-text search

Handles:
  - Embedding generation (local, using fastembed)
  - Qdrant vector store management
  - Semantic search
  - PostgreSQL full-text search
  - Hybrid search with Reciprocal Rank Fusion
"""

import uuid
import logging
from collections import defaultdict

import asyncpg
from fastembed import TextEmbedding
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct, Filter,
    FieldCondition, MatchValue, MatchAny,
)

from config import get_settings

logger = logging.getLogger(__name__)

# ── Embedding Model (local) ──────────────────────────────────────────────

_embedding_model = None


def get_embedding_model() -> TextEmbedding:
    global _embedding_model
    if _embedding_model is None:
        settings = get_settings()
        logger.info(f"Loading embedding model: {settings.embedding_model}")
        _embedding_model = TextEmbedding(settings.embedding_model)
        logger.info("Embedding model loaded")
    return _embedding_model


def embed_texts(texts: list[str]) -> list[list[float]]:
    model = get_embedding_model()
    embeddings = list(model.embed(texts))
    return [e.tolist() for e in embeddings]


def embed_query(text: str) -> list[float]:
    model = get_embedding_model()
    embeddings = list(model.query_embed(text))
    return embeddings[0].tolist()


# ── Qdrant Management ────────────────────────────────────────────────────

def get_qdrant_client() -> QdrantClient:
    settings = get_settings()
    return QdrantClient(url=settings.qdrant_url)


def ensure_collection():
    """Create the Qdrant collection if it doesn't exist."""
    settings = get_settings()
    client = get_qdrant_client()

    collections = [c.name for c in client.get_collections().collections]
    if settings.qdrant_collection not in collections:
        logger.info(f"Creating Qdrant collection: {settings.qdrant_collection}")
        client.create_collection(
            collection_name=settings.qdrant_collection,
            vectors_config=VectorParams(
                size=settings.embedding_dim,
                distance=Distance.COSINE,
            ),
        )
        logger.info("Collection created")
    else:
        logger.info(f"Qdrant collection '{settings.qdrant_collection}' already exists")


def index_chunks(document_id: str, chunks: list[dict], domain: str = None) -> list[str]:
    """Embed and index document chunks in Qdrant.

    Returns list of Qdrant point IDs.
    """
    if not chunks:
        return []

    settings = get_settings()
    client = get_qdrant_client()

    texts = [c["text"] for c in chunks]
    embeddings = embed_texts(texts)

    points = []
    point_ids = []
    for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
        point_id = str(uuid.uuid4())
        point_ids.append(point_id)

        points.append(PointStruct(
            id=point_id,
            vector=embedding,
            payload={
                "document_id": document_id,
                "chunk_index": chunk.get("chunk_index", i),
                "text": chunk["text"],
                "char_start": chunk.get("char_start", 0),
                "char_end": chunk.get("char_end", 0),
                "domain": domain or "",
            },
        ))

    # Upsert in batches
    batch_size = 100
    for i in range(0, len(points), batch_size):
        client.upsert(
            collection_name=settings.qdrant_collection,
            points=points[i:i + batch_size],
        )

    logger.info(f"Indexed {len(points)} chunks for document {document_id}")
    return point_ids


def delete_document_vectors(document_id: str):
    """Remove all vectors for a document from Qdrant."""
    settings = get_settings()
    client = get_qdrant_client()

    client.delete(
        collection_name=settings.qdrant_collection,
        points_selector=Filter(
            must=[FieldCondition(key="document_id", match=MatchValue(value=document_id))]
        ),
    )


# ── Semantic Search ──────────────────────────────────────────────────────

def semantic_search(
    query: str,
    limit: int = 10,
    domain: str = None,
    score_threshold: float = 0.3,
) -> list[dict]:
    """Search for relevant chunks using semantic similarity."""
    settings = get_settings()
    client = get_qdrant_client()

    query_embedding = embed_query(query)

    query_filter = None
    if domain:
        query_filter = Filter(
            must=[FieldCondition(key="domain", match=MatchValue(value=domain))]
        )

    results = client.query_points(
        collection_name=settings.qdrant_collection,
        query=query_embedding,
        query_filter=query_filter,
        limit=limit,
        score_threshold=score_threshold,
    )

    return [
        {
            "document_id": point.payload.get("document_id"),
            "chunk_text": point.payload.get("text", ""),
            "chunk_index": point.payload.get("chunk_index", 0),
            "domain": point.payload.get("domain", ""),
            "score": point.score,
        }
        for point in results.points
    ]


# ── Full-Text Search (PostgreSQL) ────────────────────────────────────────

async def fulltext_search(
    query: str,
    limit: int = 20,
    domain: str = None,
    db_pool: asyncpg.Pool = None,
) -> list[dict]:
    """Search documents using PostgreSQL full-text search."""
    if not db_pool:
        return []

    conditions = ["d.deleted_at IS NULL"]
    params = [query]
    param_idx = 1

    if domain:
        param_idx += 1
        conditions.append(f"d.domain = ${param_idx}")
        params.append(domain)

    param_idx += 1
    params.append(limit)

    where = " AND ".join(conditions)

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT d.id, d.title, d.domain,
                ts_rank(
                    to_tsvector('english', COALESCE(d.title, '') || ' ' || COALESCE(d.content_text, '')),
                    plainto_tsquery('english', $1)
                ) as rank
            FROM documents d
            WHERE {where}
                AND to_tsvector('english', COALESCE(d.title, '') || ' ' || COALESCE(d.content_text, ''))
                    @@ plainto_tsquery('english', $1)
            ORDER BY rank DESC
            LIMIT ${param_idx}
        """, *params)

        results = []
        for row in rows:
            doc_id = str(row["id"])
            # Get best matching chunk
            chunk_row = await conn.fetchrow("""
                SELECT chunk_text, chunk_index,
                    ts_rank(to_tsvector('english', chunk_text), plainto_tsquery('english', $1)) as chunk_rank
                FROM document_chunks
                WHERE document_id = $2
                ORDER BY chunk_rank DESC
                LIMIT 1
            """, query, row["id"])

            results.append({
                "document_id": doc_id,
                "chunk_text": chunk_row["chunk_text"] if chunk_row else "",
                "chunk_index": chunk_row["chunk_index"] if chunk_row else 0,
                "domain": row["domain"] or "",
                "score": float(row["rank"]),
            })

    return results


# ── Hybrid Search (RRF) ─────────────────────────────────────────────────

async def hybrid_search(
    query: str,
    limit: int = 10,
    domain: str = None,
    score_threshold: float = 0.3,
    db_pool: asyncpg.Pool = None,
    rrf_k: int = 60,
) -> list[dict]:
    """Hybrid search combining semantic + full-text via Reciprocal Rank Fusion."""
    fetch_limit = limit * 2

    semantic_results = semantic_search(
        query=query,
        limit=fetch_limit,
        domain=domain,
        score_threshold=score_threshold,
    )

    fulltext_results = await fulltext_search(
        query=query,
        limit=fetch_limit,
        domain=domain,
        db_pool=db_pool,
    )

    # RRF merge
    rrf_scores = defaultdict(float)
    result_data = {}
    result_sources = defaultdict(set)

    for rank, r in enumerate(semantic_results):
        key = (r["document_id"], r.get("chunk_index", 0))
        rrf_scores[key] += 1.0 / (rrf_k + rank + 1)
        result_data[key] = r
        result_sources[key].add("semantic")

    for rank, r in enumerate(fulltext_results):
        key = (r["document_id"], r.get("chunk_index", 0))
        rrf_scores[key] += 1.0 / (rrf_k + rank + 1)
        if key not in result_data:
            result_data[key] = r
        result_sources[key].add("fulltext")

    sorted_keys = sorted(rrf_scores.keys(), key=lambda k: rrf_scores[k], reverse=True)[:limit]

    results = []
    for key in sorted_keys:
        entry = result_data[key].copy()
        entry["score"] = rrf_scores[key]
        sources = result_sources[key]
        if "semantic" in sources and "fulltext" in sources:
            entry["match_type"] = "both"
        elif "fulltext" in sources:
            entry["match_type"] = "fulltext"
        else:
            entry["match_type"] = "semantic"
        results.append(entry)

    return results
