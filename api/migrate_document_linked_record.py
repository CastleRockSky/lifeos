"""
migrate_document_linked_record.py — Phase 6 of the Auto redesign.

Adds documents.linked_record_id so a document (registration, title,
insurance card, service receipt, recall notice, …) can be attached to
its vehicle (structured_record). Then runs a one-shot backfill that
matches existing auto-domain documents to active vehicles via VIN
first, year+make+model second.

Run once per database:
    docker exec lifeos-api python migrate_document_linked_record.py

Idempotent: column add and index use IF NOT EXISTS; backfill only
touches rows where linked_record_id IS NULL.
"""

import asyncio
import json
import os

import asyncpg

from auto_linking import match_document_to_vehicle


DDL = """
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS linked_record_id UUID
        REFERENCES structured_records(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_documents_linked_record
    ON documents(linked_record_id) WHERE linked_record_id IS NOT NULL;
"""


async def migrate():
    database_url = os.environ.get(
        "DATABASE_URL", "postgresql://lifeos:lifeos@postgres:5432/lifeos"
    )
    conn = await asyncpg.connect(database_url)
    try:
        print("Adding documents.linked_record_id column...")
        await conn.execute(DDL)
        print("  column + index ready.")

        # Pull active vehicles for matching.
        vehicle_rows = await conn.fetch(
            """SELECT id, data FROM structured_records
               WHERE record_type = 'vehicle' AND deleted_at IS NULL
                 AND (data->>'status' IS NULL
                      OR data->>'status' NOT IN ('merged','archived','sold','totaled'))"""
        )
        vehicles = [
            {"id": str(r["id"]),
             "data": r["data"] if isinstance(r["data"], dict) else json.loads(r["data"])}
            for r in vehicle_rows
        ]
        print(f"  candidate vehicles: {len(vehicles)}")

        # Auto-domain docs that don't have a link yet.
        doc_rows = await conn.fetch(
            """SELECT id, title, ai_extracted_data
               FROM documents
               WHERE domain = 'auto'
                 AND linked_record_id IS NULL
                 AND deleted_at IS NULL
                 AND ai_extracted_data IS NOT NULL"""
        )
        print(f"  unlinked auto docs to consider: {len(doc_rows)}")

        linked = 0
        unmatched = 0
        for row in doc_rows:
            extracted = row["ai_extracted_data"]
            if isinstance(extracted, str):
                try:
                    extracted = json.loads(extracted)
                except (TypeError, ValueError):
                    extracted = {}
            match_id = match_document_to_vehicle(extracted or {}, vehicles)
            if match_id:
                await conn.execute(
                    "UPDATE documents SET linked_record_id = $1 WHERE id = $2",
                    match_id, row["id"],
                )
                linked += 1
                print(f"    linked: {row['title']!r} → {match_id}")
            else:
                unmatched += 1
        print(f"\n  linked: {linked}   unmatched: {unmatched}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
