"""
migrate_action_metadata.py — Add an extensible metadata JSONB column to
action_items so callers can tag rows without schema changes (e.g.
`schedule_deleted=true` after a maintenance_schedule is hard-deleted).

Run once per database:
    docker exec lifeos-api python migrate_action_metadata.py

Idempotent: safe to re-run.
"""

import asyncio
import os

import asyncpg

DDL = """
ALTER TABLE action_items
    ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb;
"""


async def migrate():
    database_url = os.environ.get(
        "DATABASE_URL", "postgresql://lifeos:lifeos@postgres:5432/lifeos"
    )
    conn = await asyncpg.connect(database_url)
    try:
        print("Adding action_items.metadata column...")
        await conn.execute(DDL)
        print("  done.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
