"""
migrate_doc_suggestion.py — Schema for staged re-analysis suggestions.

Adds documents.ai_suggestion (JSONB). When a document is re-analyzed, the AI's
proposed metadata (title, domain, category, dates, summary, tags) is staged
here instead of being applied directly, so it can be reviewed and edited in
the UI before saving. Cleared once the suggestion is saved or dismissed.

Run once per database:
    docker exec lifeos-api python migrate_doc_suggestion.py

Idempotent: safe to re-run (ADD COLUMN IF NOT EXISTS).
"""

import asyncio
import os

import asyncpg

DDL = """
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS ai_suggestion JSONB;
"""


async def migrate():
    database_url = os.environ.get(
        "DATABASE_URL", "postgresql://lifeos:lifeos@postgres:5432/lifeos"
    )
    conn = await asyncpg.connect(database_url)
    try:
        print("Applying re-analysis suggestion schema...")
        await conn.execute(DDL)
        print("  documents.ai_suggestion ready.")
    finally:
        await conn.close()
    print("\nRe-analysis suggestion migration complete.")


if __name__ == "__main__":
    asyncio.run(migrate())
