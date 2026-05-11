"""
migrate_phase2.py — Add AI analysis columns to documents table.

Run once against the live database:
    docker exec lifeos-api python migrate_phase2.py
"""

import asyncio
import os

import asyncpg


async def migrate():
    database_url = os.environ.get(
        "DATABASE_URL",
        "postgresql://lifeos:lifeos@postgres:5432/lifeos",
    )

    conn = await asyncpg.connect(database_url)

    # Check which columns already exist
    existing = {
        row["column_name"]
        for row in await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'documents'"
        )
    }

    migrations = [
        ("ai_status", "ALTER TABLE documents ADD COLUMN ai_status TEXT DEFAULT 'pending'"),
        ("ai_confidence", "ALTER TABLE documents ADD COLUMN ai_confidence REAL"),
        ("review_status", "ALTER TABLE documents ADD COLUMN review_status TEXT DEFAULT 'none'"),
        ("document_date", "ALTER TABLE documents ADD COLUMN document_date DATE"),
        ("expiration_date", "ALTER TABLE documents ADD COLUMN expiration_date DATE"),
        ("ai_analyzed_at", "ALTER TABLE documents ADD COLUMN ai_analyzed_at TIMESTAMPTZ"),
        ("ai_prompt_version", "ALTER TABLE documents ADD COLUMN ai_prompt_version INTEGER DEFAULT 1"),
    ]

    applied = 0
    for col, sql in migrations:
        if col not in existing:
            await conn.execute(sql)
            print(f"  Added column: {col}")
            applied += 1
        else:
            print(f"  Column exists: {col}")

    # Add indexes for new columns
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_documents_ai_status ON documents(ai_status);
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_documents_review_status ON documents(review_status)
            WHERE review_status = 'needs_review';
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_documents_expiration ON documents(expiration_date)
            WHERE expiration_date IS NOT NULL;
    """)

    # Fix missing updated_at on action_items (trigger exists but column was missing)
    ai_existing = {
        row["column_name"]
        for row in await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'action_items'"
        )
    }
    if "updated_at" not in ai_existing:
        await conn.execute("ALTER TABLE action_items ADD COLUMN updated_at TIMESTAMPTZ DEFAULT NOW()")
        print("  Added column: action_items.updated_at")

    # Add subject_id index on action_items if missing
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_actions_subject ON action_items(subject_id);
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_actions_source_doc ON action_items(source_document_id);
    """)

    await conn.close()
    print(f"\nPhase 2 migration complete. {applied} columns added.")


if __name__ == "__main__":
    asyncio.run(migrate())
