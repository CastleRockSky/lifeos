"""
migrate_dedup.py — Schema + backfill for content deduplication.

Adds documents.file_hash, the duplicate_flags table and its indexes, then
backfills file_hash for existing documents so exact-duplicate detection works
against the back catalogue.

Run once per database:
    docker exec lifeos-api python migrate_dedup.py

Idempotent: safe to re-run (only un-hashed documents are re-read).
"""

import asyncio
import hashlib
import os

import asyncpg

DDL = """
ALTER TABLE documents ADD COLUMN IF NOT EXISTS file_hash VARCHAR(64);

CREATE INDEX IF NOT EXISTS idx_documents_file_hash
    ON documents(file_hash) WHERE file_hash IS NOT NULL;

CREATE TABLE IF NOT EXISTS duplicate_flags (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id UUID NOT NULL REFERENCES documents(id),
    duplicate_of_id UUID NOT NULL REFERENCES documents(id),
    match_type VARCHAR(20) NOT NULL,
    similarity_score REAL NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    UNIQUE (document_id, duplicate_of_id)
);

CREATE INDEX IF NOT EXISTS idx_duplicate_flags_document ON duplicate_flags(document_id);
CREATE INDEX IF NOT EXISTS idx_duplicate_flags_dup_of ON duplicate_flags(duplicate_of_id);
CREATE INDEX IF NOT EXISTS idx_duplicate_flags_pending
    ON duplicate_flags(status) WHERE status = 'pending';
"""


def file_hash(path: str) -> str:
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


async def migrate():
    database_url = os.environ.get(
        "DATABASE_URL", "postgresql://lifeos:lifeos@postgres:5432/lifeos"
    )
    conn = await asyncpg.connect(database_url)
    try:
        print("Applying dedup schema...")
        await conn.execute(DDL)
        print("  documents.file_hash + duplicate_flags ready.")

        rows = await conn.fetch(
            "SELECT id, file_path FROM documents WHERE file_hash IS NULL AND deleted_at IS NULL"
        )
        print(f"\nBackfilling file_hash for {len(rows)} document(s)...")
        hashed = missing = failed = 0
        for r in rows:
            path = r["file_path"]
            if not path or not os.path.exists(path):
                missing += 1
                continue
            try:
                await conn.execute(
                    "UPDATE documents SET file_hash = $1 WHERE id = $2",
                    file_hash(path), r["id"],
                )
                hashed += 1
            except OSError as e:
                print(f"  ! {r['id']}: {e}")
                failed += 1
        print(f"  hashed={hashed}  file-missing={missing}  failed={failed}")
    finally:
        await conn.close()
    print("\nDedup migration complete.")


if __name__ == "__main__":
    asyncio.run(migrate())
