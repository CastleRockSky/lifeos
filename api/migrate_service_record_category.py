"""
migrate_service_record_category.py — Backfill ServiceRecord.category to
'other' on existing records (Phase 4 of the Auto redesign).

Phase 4 adds a category enum to ServiceRecord (preventive | repair |
tires | bodywork | registration | inspection | other). New records default
to 'preventive'; existing AI-extracted records get 'other' since we can't
infer the right bucket retroactively.

Run once per database:
    docker exec lifeos-api python migrate_service_record_category.py

Idempotent: safe to re-run; only touches rows missing the key.
"""

import asyncio
import os

import asyncpg

DDL = """
UPDATE structured_records
SET data = jsonb_set(data, '{category}', '"other"', true)
WHERE record_type = 'service_record'
  AND deleted_at IS NULL
  AND (data ? 'category') = false;
"""


async def migrate():
    database_url = os.environ.get(
        "DATABASE_URL", "postgresql://lifeos:lifeos@postgres:5432/lifeos"
    )
    conn = await asyncpg.connect(database_url)
    try:
        before = await conn.fetchval(
            "SELECT COUNT(*) FROM structured_records "
            "WHERE record_type='service_record' AND deleted_at IS NULL"
        )
        without = await conn.fetchval(
            "SELECT COUNT(*) FROM structured_records "
            "WHERE record_type='service_record' AND deleted_at IS NULL "
            "AND (data ? 'category') = false"
        )
        print(f"service_record rows: {before}  (without category: {without})")
        result = await conn.execute(DDL)
        print(f"Backfilled: {result}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
