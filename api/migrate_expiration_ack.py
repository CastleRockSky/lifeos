"""
migrate_expiration_ack.py — Schema for acknowledging upcoming expirations.

Adds documents.expiration_acknowledged_at so a document's expiration alert can
be cleared from the dashboard without touching the expiration_date itself.
The /api/expirations list hides any document with this column set.

Run once per database:
    docker exec lifeos-api python migrate_expiration_ack.py

Idempotent: safe to re-run (ADD COLUMN IF NOT EXISTS).
"""

import asyncio
import os

import asyncpg

DDL = """
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS expiration_acknowledged_at TIMESTAMPTZ;
"""


async def migrate():
    database_url = os.environ.get(
        "DATABASE_URL", "postgresql://lifeos:lifeos@postgres:5432/lifeos"
    )
    conn = await asyncpg.connect(database_url)
    try:
        print("Applying expiration-acknowledgement schema...")
        await conn.execute(DDL)
        print("  documents.expiration_acknowledged_at ready.")
    finally:
        await conn.close()
    print("\nExpiration-acknowledgement migration complete.")


if __name__ == "__main__":
    asyncio.run(migrate())
