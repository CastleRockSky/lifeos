"""
migrate_tax.py — Re-home existing tax records under the new 'tax' domain.

Tax-related categories used to live in the 'financial' domain. This script
moves existing data so the new Tax dashboard sees them.

Run once per database:
    docker exec lifeos-api python migrate_tax.py

Idempotent: safe to re-run.
"""

import asyncio
import os

import asyncpg

# Document categories that originally lived under 'financial' but are now
# part of the 'tax' domain.
TAX_CATEGORIES = ("tax_return", "w2", "1099", "tax_estimate")


async def migrate():
    database_url = os.environ.get(
        "DATABASE_URL",
        "postgresql://lifeos:lifeos@postgres:5432/lifeos",
    )

    conn = await asyncpg.connect(database_url)
    try:
        # 1) Documents
        n_docs = await conn.fetchval("""
            UPDATE documents
            SET domain = 'tax'
            WHERE domain = 'financial'
              AND category = ANY($1::text[])
              AND deleted_at IS NULL
            RETURNING 1
        """, list(TAX_CATEGORIES))
        # asyncpg returns a single value from RETURNING — for an aggregate count
        # we need a separate count query.
        rows_docs = await conn.fetch("""
            SELECT id, title, category FROM documents
            WHERE domain = 'tax' AND category = ANY($1::text[]) AND deleted_at IS NULL
        """, list(TAX_CATEGORIES))
        print(f"  Documents now in 'tax' domain: {len(rows_docs)}")
        for r in rows_docs[:5]:
            print(f"    - {r['category']:14s} {r['title'][:60]}")
        if len(rows_docs) > 5:
            print(f"    ... and {len(rows_docs) - 5} more")

        # 2) Structured records (tax_item)
        sr_count = await conn.fetchval("""
            WITH moved AS (
                UPDATE structured_records
                SET domain = 'tax'
                WHERE record_type = 'tax_item'
                  AND (domain = 'financial' OR domain IS NULL)
                  AND deleted_at IS NULL
                RETURNING id
            )
            SELECT COUNT(*) FROM moved
        """)
        print(f"  Structured records (tax_item) re-domained: {sr_count}")

        # 3) Action items linked to tax_item records or tax-domain documents
        ai_count = await conn.fetchval("""
            WITH moved AS (
                UPDATE action_items
                SET domain = 'tax'
                WHERE deleted_at IS NULL
                  AND domain != 'tax'
                  AND (
                    source_record_id IN (
                        SELECT id FROM structured_records
                        WHERE record_type = 'tax_item'
                          AND deleted_at IS NULL
                    )
                    OR source_document_id IN (
                        SELECT id FROM documents
                        WHERE domain = 'tax' AND deleted_at IS NULL
                    )
                  )
                RETURNING id
            )
            SELECT COUNT(*) FROM moved
        """)
        print(f"  Action items re-domained: {ai_count}")

        # 4) Email sender map entries
        em_count = await conn.fetchval("""
            WITH moved AS (
                UPDATE email_sender_map
                SET domain = 'tax'
                WHERE domain = 'financial'
                  AND category = ANY($1::text[])
                RETURNING id
            )
            SELECT COUNT(*) FROM moved
        """, list(TAX_CATEGORIES))
        print(f"  Email sender mappings re-domained: {em_count}")

    finally:
        await conn.close()
    print("\nTax migration complete.")


if __name__ == "__main__":
    asyncio.run(migrate())
