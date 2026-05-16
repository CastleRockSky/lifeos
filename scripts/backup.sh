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
# Optional off-site upload to Azure Blob Storage (when AZURE_STORAGE_ACCOUNT,
# AZURE_CONTAINER, AZURE_SAS_TOKEN are set) and/or Backblaze B2 (when B2_BUCKET,
# B2_KEY_ID, B2_APPLICATION_KEY are set). Both read from repo /.env; each leg
# is independent and non-fatal.
#
# Run via cron, e.g. nightly:
#   0 3 * * * /opt/lifeos/scripts/backup.sh >> /var/log/lifeos-backup.log 2>&1

set -euo pipefail

# Pull in repo-level .env if present, so cron-invoked runs see Azure creds
# without needing them in /etc/cron.d/lifeos-backup.
#
# Note: we DON'T `source` the file. docker-compose's .env format is a
# literal KEY=VALUE list with no shell expansion, so values containing
# `&` (like SAS tokens) or other shell metacharacters break `bash -c`
# sourcing. Roll our own docker-compose-compatible parser instead.
load_env() {
    local file="$1"
    [ -f "$file" ] || return 0
    local line key value
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line#"${line%%[![:space:]]*}"}"   # strip leading whitespace
        [ -z "$line" ] && continue
        [ "${line:0:1}" = "#" ] && continue
        [ "${line:0:7}" = "export " ] && line="${line:7}"
        case "$line" in *=*) ;; *) continue;; esac
        key="${line%%=*}"
        value="${line#*=}"
        # Strip a single matched pair of surrounding quotes, if present.
        if [ "${value:0:1}" = '"' ] && [ "${value: -1}" = '"' ]; then
            value="${value:1:${#value}-2}"
        elif [ "${value:0:1}" = "'" ] && [ "${value: -1}" = "'" ]; then
            value="${value:1:${#value}-2}"
        fi
        export "$key=$value"
    done < "$file"
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
load_env "$REPO_DIR/.env"

DATA_PATH="${DATA_PATH:-/srv/lifeos}"
BACKUP_DIR="${BACKUP_DIR:-${DATA_PATH}/backups}"
RETAIN_COUNT="${RETAIN_COUNT:-14}"
PG_CONTAINER="${PG_CONTAINER:-lifeos-postgres}"
QDRANT_URL="${QDRANT_URL:-http://localhost:6334}"
QDRANT_COLLECTION="${QDRANT_COLLECTION:-documents}"
POSTGRES_DB="${POSTGRES_DB:-lifeos}"
POSTGRES_USER="${POSTGRES_USER:-lifeos}"
AZURE_STORAGE_ACCOUNT="${AZURE_STORAGE_ACCOUNT:-}"
AZURE_CONTAINER="${AZURE_CONTAINER:-}"
AZURE_SAS_TOKEN="${AZURE_SAS_TOKEN:-}"
AZURE_BLOB_PREFIX="${AZURE_BLOB_PREFIX:-}"  # optional path prefix inside the container
B2_BUCKET="${B2_BUCKET:-}"
B2_KEY_ID="${B2_KEY_ID:-}"
B2_APPLICATION_KEY="${B2_APPLICATION_KEY:-}"
B2_PREFIX="${B2_PREFIX:-}"  # optional path prefix inside the bucket

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

# ── Azure Blob upload (optional, off-site) ──────────────────────────────
if [ -n "$AZURE_STORAGE_ACCOUNT" ] && [ -n "$AZURE_CONTAINER" ] && [ -n "$AZURE_SAS_TOKEN" ]; then
    if ! command -v azcopy >/dev/null 2>&1; then
        echo "  ! Azure upload skipped: azcopy not installed (run scripts/install-backup-cron.sh)"
    else
        BLOB_NAME="$(basename "$ARCHIVE")"
        [ -n "$AZURE_BLOB_PREFIX" ] && BLOB_NAME="${AZURE_BLOB_PREFIX%/}/$BLOB_NAME"
        SAS="${AZURE_SAS_TOKEN#\?}"  # tolerate leading '?'
        DEST_URL="https://${AZURE_STORAGE_ACCOUNT}.blob.core.windows.net/${AZURE_CONTAINER}/${BLOB_NAME}?${SAS}"
        echo "  - Uploading to Azure (${AZURE_STORAGE_ACCOUNT}/${AZURE_CONTAINER}/${BLOB_NAME})..."
        # AZCOPY_LOG_LOCATION defaults to ~/.azcopy which doesn't exist for cron's
        # root; redirect to /tmp so azcopy doesn't fail on its own log setup.
        if AZCOPY_LOG_LOCATION="${AZCOPY_LOG_LOCATION:-/tmp}" \
           AZCOPY_JOB_PLAN_LOCATION="${AZCOPY_JOB_PLAN_LOCATION:-/tmp}" \
               azcopy copy "$ARCHIVE" "$DEST_URL" --log-level=ERROR --output-level=quiet; then
            echo "    ✓ Uploaded $BLOB_NAME"
        else
            echo "    ! Azure upload failed (azcopy exit $?). Local backup preserved." >&2
        fi
    fi
fi

# ── Backblaze B2 upload (optional, off-site) ────────────────────────────
if [ -n "$B2_BUCKET" ] && [ -n "$B2_KEY_ID" ] && [ -n "$B2_APPLICATION_KEY" ]; then
    if ! command -v rclone >/dev/null 2>&1; then
        echo "  ! Backblaze upload skipped: rclone not installed (run scripts/install-backup-cron.sh)"
    else
        B2_DEST=":b2:${B2_BUCKET}"
        [ -n "$B2_PREFIX" ] && B2_DEST="${B2_DEST}/${B2_PREFIX#/}"
        echo "  - Uploading to Backblaze B2 (${B2_BUCKET}/${B2_PREFIX})..."
        # On-the-fly remote (:b2:) with creds passed as flags — no rclone.conf
        # needed. --config /dev/null keeps it from reading/writing a user config.
        if rclone copy "$ARCHIVE" "$B2_DEST" \
               --b2-account="$B2_KEY_ID" --b2-key="$B2_APPLICATION_KEY" \
               --config /dev/null --log-level ERROR; then
            echo "    ✓ Uploaded $(basename "$ARCHIVE") to B2"
        else
            echo "    ! Backblaze upload failed (rclone exit $?). Local backup preserved." >&2
        fi
    fi
fi

# ── Retention (local) ───────────────────────────────────────────────────
ls -1t "${BACKUP_DIR}"/lifeos-*.tar.gz 2>/dev/null | tail -n "+$((RETAIN_COUNT + 1))" | \
    while read -r old; do
        echo "  - Pruning $old"
        rm -f "$old"
    done

echo "[$(date -Iseconds)] Backup complete."
