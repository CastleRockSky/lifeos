"""
migrate_phase3.py — Phase 3 schema additions: email ingestion.

Adds:
  - email_messages table
  - email_sender_map table
  - documents.email_message_id column + FK
  - supporting indexes and triggers

Run once against the live database:
    docker exec lifeos-api python migrate_phase3.py
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

    existing_tables = {
        row["table_name"]
        for row in await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
    }

    if "email_messages" not in existing_tables:
        await conn.execute("""
            CREATE TABLE email_messages (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                message_id VARCHAR(998) UNIQUE,
                imap_uid BIGINT,
                sender VARCHAR(500),
                original_sender VARCHAR(500),
                recipient VARCHAR(500),
                subject TEXT,
                clean_subject TEXT,
                body_text TEXT,
                body_html TEXT,
                received_at TIMESTAMPTZ,
                attachment_count INTEGER DEFAULT 0,
                document_count INTEGER DEFAULT 0,
                status VARCHAR(50) DEFAULT 'pending',
                error_message TEXT,
                retry_count INTEGER DEFAULT 0,
                raw_size_bytes BIGINT,
                domain_hint VARCHAR(50),
                category_hint VARCHAR(100),
                subject_hint VARCHAR(255),
                processed_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        print("  Created table: email_messages")
    else:
        print("  Table exists: email_messages")

    if "email_sender_map" not in existing_tables:
        await conn.execute("""
            CREATE TABLE email_sender_map (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                sender_pattern VARCHAR(500) NOT NULL UNIQUE,
                domain VARCHAR(50),
                category VARCHAR(100),
                subject_hint VARCHAR(255),
                notes TEXT,
                auto_learned BOOLEAN DEFAULT false,
                confidence REAL DEFAULT 0.0,
                match_count INTEGER DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                last_matched_at TIMESTAMPTZ
            )
        """)
        print("  Created table: email_sender_map")
    else:
        print("  Table exists: email_sender_map")

    # documents.email_message_id
    doc_columns = {
        row["column_name"]
        for row in await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'documents'"
        )
    }
    if "email_message_id" not in doc_columns:
        await conn.execute("ALTER TABLE documents ADD COLUMN email_message_id UUID")
        print("  Added column: documents.email_message_id")
    else:
        print("  Column exists: documents.email_message_id")

    # FK (idempotent: check pg_constraint)
    fk_exists = await conn.fetchval("""
        SELECT 1 FROM pg_constraint
        WHERE conname = 'documents_email_message_id_fkey'
    """)
    if not fk_exists:
        await conn.execute("""
            ALTER TABLE documents
            ADD CONSTRAINT documents_email_message_id_fkey
            FOREIGN KEY (email_message_id) REFERENCES email_messages(id) ON DELETE SET NULL
        """)
        print("  Added FK: documents.email_message_id → email_messages.id")
    else:
        print("  FK exists: documents.email_message_id")

    # Indexes (CREATE INDEX IF NOT EXISTS)
    indexes = [
        ("idx_email_messages_status", "CREATE INDEX IF NOT EXISTS idx_email_messages_status ON email_messages(status)"),
        ("idx_email_messages_received", "CREATE INDEX IF NOT EXISTS idx_email_messages_received ON email_messages(received_at DESC)"),
        ("idx_email_messages_sender", "CREATE INDEX IF NOT EXISTS idx_email_messages_sender ON email_messages(sender)"),
        ("idx_documents_email_message", "CREATE INDEX IF NOT EXISTS idx_documents_email_message ON documents(email_message_id) WHERE email_message_id IS NOT NULL"),
        ("idx_email_sender_map_pattern", "CREATE INDEX IF NOT EXISTS idx_email_sender_map_pattern ON email_sender_map(sender_pattern)"),
    ]
    for name, sql in indexes:
        await conn.execute(sql)
        print(f"  Ensured index: {name}")

    # Triggers (idempotent: drop + create)
    for trig in ("email_messages_updated_at", "email_sender_map_updated_at"):
        table = trig.replace("_updated_at", "")
        await conn.execute(f"DROP TRIGGER IF EXISTS {trig} ON {table}")
        await conn.execute(
            f"CREATE TRIGGER {trig} BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION update_updated_at()"
        )
        print(f"  Ensured trigger: {trig}")

    await conn.close()
    print("\nPhase 3 migration complete.")


if __name__ == "__main__":
    asyncio.run(migrate())
