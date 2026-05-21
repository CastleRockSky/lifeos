"""
migrate_document_notes.py — Schema for user notes on documents.

Adds documents.notes (TEXT). This is a free-text field where the user can
record additional context or thoughts about a document. Notes are included
in both keyword search (PostgreSQL tsvector) and semantic search (embedded
as Qdrant points tagged kind="notes").

Run once per database:
    docker exec lifeos-api python migrate_document_notes.py

Idempotent: safe to re-run (ADD COLUMN IF NOT EXISTS).
"""

import asyncio
import os

import asyncpg

DDL = """
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS notes TEXT;
"""


async def migrate():
    database_url = os.environ.get(
        "DATABASE_URL", "postgresql://lifeos:lifeos@postgres:5432/lifeos"
    )
    conn = await asyncpg.connect(database_url)
    try:
        print("Applying document-notes schema...")
        await conn.execute(DDL)
        print("  documents.notes ready.")
    finally:
        await conn.close()
    print("\nDocument-notes migration complete.")


if __name__ == "__main__":
    asyncio.run(migrate())
