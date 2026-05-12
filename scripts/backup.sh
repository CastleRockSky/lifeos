#!/usr/bin/env bash
# backup.sh — Snapshot the LifeOS persistent state.
#
# Backs up:
#   - PostgreSQL (pg_dump of the lifeos database)
#   - Qdrant snapshot (via the HTTP snapshot API)
#   - Document files (/srv/lifeos/documents/files)
#   - Auth tokens (/srv/lifeos/auth)
#
# Output: $BACKUP_DIR/lifeos-YYYYMMDD-HHMMSS.tar.gz
# Retention: keeps the most recent $RETAIN_COUNT backups (default 14).
#
# Run via cron, e.g. nightly:
#   0 3 * * * /opt/lifeos/scripts/backup.sh >> /var/log/lifeos-backup.log 2>&1

set -euo pipefail

DATA_PATH="${DATA_PATH:-/srv/lifeos}"
BACKUP_DIR="${BACKUP_DIR:-${DATA_PATH}/backups}"
RETAIN_COUNT="${RETAIN_COUNT:-14}"
PG_CONTAINER="${PG_CONTAINER:-lifeos-postgres}"
QDRANT_URL="${QDRANT_URL:-http://localhost:6334}"
QDRANT_COLLECTION="${QDRANT_COLLECTION:-documents}"
POSTGRES_DB="${POSTGRES_DB:-lifeos}"
POSTGRES_USER="${POSTGRES_USER:-lifeos}"

TS="$(date -u +%Y%m%d-%H%M%S)"
WORK="$(mktemp -d -t lifeos-backup-XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$BACKUP_DIR"

echo "[$(date -Iseconds)] Starting LifeOS backup → ${BACKUP_DIR}/lifeos-${TS}.tar.gz"

# ── PostgreSQL dump ─────────────────────────────────────────────────────
echo "  - Dumping PostgreSQL..."
docker exec -t "$PG_CONTAINER" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    --no-owner --no-privileges \
    > "$WORK/postgres.sql"

# ── Qdrant snapshot ─────────────────────────────────────────────────────
echo "  - Snapshotting Qdrant..."
SNAPSHOT_NAME="$(curl -fsS -X POST "${QDRANT_URL}/collections/${QDRANT_COLLECTION}/snapshots" \
    | grep -oE '"name":"[^"]+"' | head -1 | sed 's/.*"name":"\([^"]*\)".*/\1/')"
if [ -n "$SNAPSHOT_NAME" ]; then
    curl -fsS -o "$WORK/qdrant-snapshot.snapshot" \
        "${QDRANT_URL}/collections/${QDRANT_COLLECTION}/snapshots/${SNAPSHOT_NAME}"
fi

# ── File trees ──────────────────────────────────────────────────────────
echo "  - Archiving document files + auth tokens..."
mkdir -p "$WORK/files" "$WORK/auth"
if [ -d "${DATA_PATH}/documents/files" ]; then
    cp -a "${DATA_PATH}/documents/files/." "$WORK/files/" || true
fi
if [ -d "${DATA_PATH}/auth" ]; then
    cp -a "${DATA_PATH}/auth/." "$WORK/auth/" || true
fi

# ── Bundle ──────────────────────────────────────────────────────────────
ARCHIVE="${BACKUP_DIR}/lifeos-${TS}.tar.gz"
tar -czf "$ARCHIVE" -C "$WORK" .
chmod 600 "$ARCHIVE"
echo "  - Wrote $(du -h "$ARCHIVE" | cut -f1) → $ARCHIVE"

# ── Retention ───────────────────────────────────────────────────────────
ls -1t "${BACKUP_DIR}"/lifeos-*.tar.gz 2>/dev/null | tail -n "+$((RETAIN_COUNT + 1))" | \
    while read -r old; do
        echo "  - Pruning $old"
        rm -f "$old"
    done

echo "[$(date -Iseconds)] Backup complete."
