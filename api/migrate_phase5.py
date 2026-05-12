"""
migrate_phase5.py — Phase 5 schema additions: medical module.

Adds:
  - medication_doses table (adherence log)
  - supporting indexes

Run once against the live database:
    docker exec lifeos-api python migrate_phase5.py
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

    if "medication_doses" not in existing_tables:
        await conn.execute("""
            CREATE TABLE medication_doses (
                id BIGSERIAL PRIMARY KEY,
                medication_record_id UUID NOT NULL REFERENCES structured_records(id) ON DELETE CASCADE,
                subject_id UUID REFERENCES subjects(id),
                scheduled_at TIMESTAMPTZ,
                recorded_at TIMESTAMPTZ NOT NULL,
                status VARCHAR(20) NOT NULL,
                notes TEXT,
                source VARCHAR(50) DEFAULT 'agent_api',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        print("  Created table: medication_doses")
    else:
        print("  Table exists: medication_doses")

    indexes = [
        ("idx_med_doses_record", "CREATE INDEX IF NOT EXISTS idx_med_doses_record ON medication_doses(medication_record_id, recorded_at DESC)"),
        ("idx_med_doses_subject", "CREATE INDEX IF NOT EXISTS idx_med_doses_subject ON medication_doses(subject_id, recorded_at DESC)"),
    ]
    for name, sql in indexes:
        await conn.execute(sql)
        print(f"  Ensured index: {name}")

    await conn.close()
    print("\nPhase 5 migration complete.")


if __name__ == "__main__":
    asyncio.run(migrate())
