"""
migrate_document_subjects.py — Multi-subject support for documents.

Adds the document_subjects junction table so a single document can be
associated with multiple subjects (e.g. a divorce filing that concerns
both parents and the children). The legacy documents.subject_id column
stays in place as the "primary subject" denormalisation — it always
matches the junction row where is_primary = true.

Run once per database:
    docker exec lifeos-api python migrate_document_subjects.py

Idempotent: safe to re-run.
"""

import asyncio
import os

import asyncpg

DDL = """
CREATE TABLE IF NOT EXISTS document_subjects (
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    subject_id  UUID NOT NULL REFERENCES subjects(id)  ON DELETE CASCADE,
    is_primary  BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (document_id, subject_id)
);

CREATE INDEX IF NOT EXISTS idx_document_subjects_subject
    ON document_subjects(subject_id);

-- At most one primary subject per document.
CREATE UNIQUE INDEX IF NOT EXISTS idx_document_subjects_primary
    ON document_subjects(document_id) WHERE is_primary;
"""

# Keep documents.subject_id and the junction's primary row in sync, so existing
# code paths that write to documents.subject_id (the upload form, the
# background AI subject resolver, etc.) don't need to know about the junction.
# Secondary subjects must still be added via the API helper.
TRIGGER = """
CREATE OR REPLACE FUNCTION sync_documents_primary_subject() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.subject_id IS DISTINCT FROM OLD.subject_id THEN
        IF NEW.subject_id IS NULL THEN
            DELETE FROM document_subjects
            WHERE document_id = NEW.id AND is_primary;
        ELSE
            UPDATE document_subjects
               SET is_primary = false
             WHERE document_id = NEW.id
               AND is_primary
               AND subject_id <> NEW.subject_id;

            INSERT INTO document_subjects (document_id, subject_id, is_primary)
                VALUES (NEW.id, NEW.subject_id, true)
                ON CONFLICT (document_id, subject_id)
                DO UPDATE SET is_primary = true;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS documents_sync_primary_subject ON documents;
CREATE TRIGGER documents_sync_primary_subject
AFTER INSERT OR UPDATE OF subject_id ON documents
FOR EACH ROW EXECUTE FUNCTION sync_documents_primary_subject();
"""

BACKFILL = """
INSERT INTO document_subjects (document_id, subject_id, is_primary)
SELECT d.id, d.subject_id, true
FROM documents d
WHERE d.subject_id IS NOT NULL
  AND d.deleted_at IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM document_subjects ds
      WHERE ds.document_id = d.id AND ds.subject_id = d.subject_id
  );
"""


async def migrate():
    database_url = os.environ.get(
        "DATABASE_URL", "postgresql://lifeos:lifeos@postgres:5432/lifeos"
    )
    conn = await asyncpg.connect(database_url)
    try:
        print("Creating document_subjects junction table...")
        await conn.execute(DDL)
        print("  table + indexes ready.")

        print("Backfilling from documents.subject_id...")
        result = await conn.execute(BACKFILL)
        print(f"  {result}")

        print("Installing sync trigger on documents.subject_id...")
        await conn.execute(TRIGGER)
        print("  trigger ready.")
    finally:
        await conn.close()
    print("\nMulti-subject migration complete.")


if __name__ == "__main__":
    asyncio.run(migrate())
