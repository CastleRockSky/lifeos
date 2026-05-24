"""
migrate_jsonb_unwrap.py — Fix the long-standing double-encoded-jsonb bug.

asyncpg's JSONB codec auto-encodes Python values via json.dumps. Several
code paths (records.py POST/PATCH, agent_finance, vehicles log_mileage,
etc.) also called json.dumps() *before* handing the value to asyncpg, so
the codec encoded the already-JSON-encoded string a second time — every
structured_records.data ended up as a JSONB *string scalar* containing
the JSON text instead of as a JSONB *object*.

Symptoms: jsonb_typeof(data) = 'string' for every row. JSONB key access
(data->>'status', etc.) returns NULL. The frontend masked the issue by
json.loads()-ing the value in _serialise.

Fix: unwrap any row where data is a JSONB string scalar by extracting its
text via `data #>> '{}'` and casting back to jsonb. Rows already shaped as
objects are left alone.

Idempotent: safe to re-run.

Run once per database:
    docker exec lifeos-api python migrate_jsonb_unwrap.py
"""

import asyncio
import os

import asyncpg


async def migrate():
    database_url = os.environ.get(
        "DATABASE_URL", "postgresql://lifeos:lifeos@postgres:5432/lifeos"
    )
    conn = await asyncpg.connect(database_url)
    try:
        before = await conn.fetch(
            "SELECT jsonb_typeof(data) AS t, COUNT(*) FROM structured_records "
            "WHERE deleted_at IS NULL GROUP BY jsonb_typeof(data) ORDER BY t"
        )
        print("Before:")
        for r in before:
            print(f"  {r['t']}: {r['count']}")

        result = await conn.execute(
            """UPDATE structured_records
               SET data = (data #>> '{}')::jsonb
               WHERE jsonb_typeof(data) = 'string'"""
        )
        print(f"\nUnwrapped: {result}")

        after = await conn.fetch(
            "SELECT jsonb_typeof(data) AS t, COUNT(*) FROM structured_records "
            "WHERE deleted_at IS NULL GROUP BY jsonb_typeof(data) ORDER BY t"
        )
        print("\nAfter:")
        for r in after:
            print(f"  {r['t']}: {r['count']}")
    finally:
        await conn.close()
    print("\nJSONB unwrap migration complete.")


if __name__ == "__main__":
    asyncio.run(migrate())
