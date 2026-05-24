"""
migrate_action_source_fk.py — Switch action_items.source_record_id FK to
ON DELETE SET NULL.

Phase 3 of the Auto redesign hard-deletes maintenance_schedule rows while
keeping the action items they produced (tagged with metadata.schedule_deleted
= true). The original RESTRICT-by-default FK blocked that. SET NULL lets
the action items survive with their tagged metadata; the UI can still show
"abandoned-on-purpose" vs "forgotten" via the metadata flag.

Run once per database:
    docker exec lifeos-api python migrate_action_source_fk.py

Idempotent: safe to re-run.
"""

import asyncio
import os

import asyncpg

DDL = """
ALTER TABLE action_items
    DROP CONSTRAINT IF EXISTS action_items_source_record_id_fkey;

ALTER TABLE action_items
    ADD CONSTRAINT action_items_source_record_id_fkey
        FOREIGN KEY (source_record_id)
        REFERENCES structured_records(id)
        ON DELETE SET NULL;
"""


async def migrate():
    database_url = os.environ.get(
        "DATABASE_URL", "postgresql://lifeos:lifeos@postgres:5432/lifeos"
    )
    conn = await asyncpg.connect(database_url)
    try:
        print("Switching action_items.source_record_id FK to ON DELETE SET NULL...")
        await conn.execute(DDL)
        print("  done.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
