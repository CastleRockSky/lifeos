#!/usr/bin/env bash
# restore.sh — Restore LifeOS state from a backup tarball.
#
# Interactive, per-component disaster recovery — the automated counterpart
# to the manual procedure in docs/BACKUP-SETUP.md. Restores any subset of:
#   - PostgreSQL  (drop + recreate the lifeos database, load the dump)
#   - Qdrant      (upload + recover the collection snapshot)
#   - Document files  (/srv/lifeos/documents/files)
#   - Auth tokens     (/srv/lifeos/auth)
#
# Each component is confirmed separately, so a partial restore is safe.
#
# Usage:
#   sudo scripts/restore.sh [BACKUP.tar.gz]
#
# With no argument it lists the local backups in $BACKUP_DIR to pick from,
# and (if Backblaze B2 is configured in .env) offers to pull the newest
# off-site copy instead.

set -euo pipefail

# ── .env loader ─────────────────────────────────────────────────────────
# Same docker-compose-compatible parser backup.sh ships with (don't `source`
# — SAS tokens etc. contain shell metacharacters).
load_env() {
    local file="$1"
    [ -f "$file" ] || return 0
    local line key value
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line#"${line%%[![:space:]]*}"}"
        [ -z "$line" ] && continue
        [ "${line:0:1}" = "#" ] && continue
        [ "${line:0:7}" = "export " ] && line="${line:7}"
        case "$line" in *=*) ;; *) continue;; esac
        key="${line%%=*}"
        value="${line#*=}"
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
PG_CONTAINER="${PG_CONTAINER:-lifeos-postgres}"
API_CONTAINER="${API_CONTAINER:-lifeos-api}"
QDRANT_CONTAINER="${QDRANT_CONTAINER:-lifeos-qdrant}"
QDRANT_URL="${QDRANT_URL:-http://localhost:6334}"
QDRANT_COLLECTION="${QDRANT_COLLECTION:-documents}"
POSTGRES_DB="${POSTGRES_DB:-lifeos}"
POSTGRES_USER="${POSTGRES_USER:-lifeos}"
# Host-mapped API port — LifeOS is shifted to :8100 (Ezekiel holds :8000).
API_HEALTH_URL="${API_HEALTH_URL:-http://127.0.0.1:8100/api/health}"
API_STATS_URL="${API_STATS_URL:-http://127.0.0.1:8100/api/stats}"
B2_BUCKET="${B2_BUCKET:-}"
B2_KEY_ID="${B2_KEY_ID:-}"
B2_APPLICATION_KEY="${B2_APPLICATION_KEY:-}"
B2_PREFIX="${B2_PREFIX:-}"

COMPOSE=(docker compose -f "$REPO_DIR/docker-compose.yml")

log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
err()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $*" >&2; }
die()  { err "$*"; exit 1; }
confirm() {
    local reply
    read -rp "$1 [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]]
}

echo "============================================="
echo "  LifeOS — Restore"
echo "============================================="
echo

# ── Pre-flight ──────────────────────────────────────────────────────────
[ "$EUID" -eq 0 ] || die "must be run as root (use sudo) — it writes to ${DATA_PATH} and drives docker."
command -v docker >/dev/null 2>&1 || die "docker not found."

# ── Select the backup tarball ───────────────────────────────────────────
TARBALL="${1:-}"

if [ -n "$TARBALL" ]; then
    [ -f "$TARBALL" ] || die "backup file not found: $TARBALL"
else
    echo "No backup file given. Local backups in ${BACKUP_DIR}:"
    echo
    mapfile -t LOCAL < <(ls -1t "${BACKUP_DIR}"/lifeos-*.tar.gz 2>/dev/null || true)
    if [ "${#LOCAL[@]}" -gt 0 ]; then
        for i in "${!LOCAL[@]}"; do
            printf "  [%d] %s  (%s)\n" "$((i + 1))" "$(basename "${LOCAL[$i]}")" \
                "$(du -h "${LOCAL[$i]}" | cut -f1)"
        done
    else
        echo "  (none found locally)"
    fi
    echo
    B2_READY=0
    if [ -n "$B2_BUCKET" ] && [ -n "$B2_KEY_ID" ] && [ -n "$B2_APPLICATION_KEY" ] \
       && command -v rclone >/dev/null 2>&1; then
        B2_READY=1
        echo "  [b] pull the newest backup from Backblaze B2 (${B2_BUCKET})"
        echo
    fi
    read -rp "Select a backup (number${B2_READY:+ or 'b'}): " CHOICE

    if [ "$B2_READY" = "1" ] && [[ "$CHOICE" =~ ^[Bb]$ ]]; then
        B2_SRC=":b2:${B2_BUCKET}"
        [ -n "$B2_PREFIX" ] && B2_SRC="${B2_SRC}/${B2_PREFIX#/}"
        NEWEST="$(rclone lsf "$B2_SRC" \
            --b2-account="$B2_KEY_ID" --b2-key="$B2_APPLICATION_KEY" --config /dev/null 2>/dev/null \
            | grep -E '^lifeos-[0-9]{8}-[0-9]{6}\.tar\.gz$' | sort | tail -1 || true)"
        [ -n "$NEWEST" ] || die "no lifeos-*.tar.gz objects found in B2."
        mkdir -p "$BACKUP_DIR"
        log "Downloading $NEWEST from B2..."
        rclone copy "${B2_SRC}/${NEWEST}" "$BACKUP_DIR" \
            --b2-account="$B2_KEY_ID" --b2-key="$B2_APPLICATION_KEY" \
            --config /dev/null --progress || die "B2 download failed."
        TARBALL="${BACKUP_DIR}/${NEWEST}"
    else
        [[ "$CHOICE" =~ ^[0-9]+$ ]] || die "invalid selection."
        IDX=$((CHOICE - 1))
        [ "$IDX" -ge 0 ] && [ "$IDX" -lt "${#LOCAL[@]}" ] || die "selection out of range."
        TARBALL="${LOCAL[$IDX]}"
    fi
fi

log "Restoring from: $TARBALL"
echo

# ── Inspect the tarball ─────────────────────────────────────────────────
MEMBERS="$(tar -tzf "$TARBALL")"
has_member() { printf '%s\n' "$MEMBERS" | grep -qE "^\./?$1(/|\$)"; }

echo "Backup contents:"
has_member 'postgres.sql'            && echo "  - PostgreSQL dump"
has_member 'qdrant-snapshot.snapshot' && echo "  - Qdrant snapshot"
has_member 'files'                   && echo "  - Document files"
has_member 'auth'                    && echo "  - Auth tokens"
echo

EXTRACT="$(mktemp -d -t lifeos-restore-XXXXXX)"
trap 'rm -rf "$EXTRACT"' EXIT
# Pull one member (file or dir) out of the tarball into $EXTRACT.
extract_member() { tar -xzf "$TARBALL" -C "$EXTRACT" "./$1" 2>/dev/null || tar -xzf "$TARBALL" -C "$EXTRACT" "$1"; }

require_container() {
    docker ps --format '{{.Names}}' | grep -qx "$1" && return 0
    echo "Container '$1' is not running."
    if confirm "Start the stack ('docker compose up -d')?"; then
        "${COMPOSE[@]}" up -d
        log "Waiting for containers to settle..."
        sleep 8
    fi
    docker ps --format '{{.Names}}' | grep -qx "$1" \
        || die "container '$1' still not running — cannot continue."
}

# ── 1. PostgreSQL ───────────────────────────────────────────────────────
if has_member 'postgres.sql'; then
    echo "--- PostgreSQL ---"
    echo "WARNING: this DROPS and recreates the '${POSTGRES_DB}' database."
    echo "All current PostgreSQL data is replaced."
    echo
    if confirm "Restore PostgreSQL?"; then
        require_container "$PG_CONTAINER"
        log "Stopping the API container so it can't write mid-restore..."
        "${COMPOSE[@]}" stop api >/dev/null 2>&1 || true
        API_WAS_STOPPED=1

        log "Extracting dump..."
        extract_member 'postgres.sql'

        log "Dropping and recreating '${POSTGRES_DB}'..."
        docker exec -i "$PG_CONTAINER" psql -U "$POSTGRES_USER" -d postgres \
            -v ON_ERROR_STOP=1 \
            -c "DROP DATABASE IF EXISTS ${POSTGRES_DB} WITH (FORCE);" \
            -c "CREATE DATABASE ${POSTGRES_DB};"

        log "Loading dump..."
        docker exec -i "$PG_CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
            -v ON_ERROR_STOP=1 --single-transaction < "$EXTRACT/postgres.sql"
        log "PostgreSQL restore complete."
    else
        log "Skipped PostgreSQL."
    fi
    echo
fi

# ── 2. Qdrant ───────────────────────────────────────────────────────────
if has_member 'qdrant-snapshot.snapshot'; then
    echo "--- Qdrant ---"
    echo "Uploads the snapshot and recovers the '${QDRANT_COLLECTION}' collection,"
    echo "replacing its current vectors."
    echo
    if confirm "Restore Qdrant?"; then
        require_container "$QDRANT_CONTAINER"
        log "Extracting snapshot..."
        extract_member 'qdrant-snapshot.snapshot'
        log "Uploading snapshot to Qdrant..."
        curl -fsS -X POST \
            "${QDRANT_URL}/collections/${QDRANT_COLLECTION}/snapshots/upload?priority=snapshot" \
            -H 'Content-Type: multipart/form-data' \
            -F "snapshot=@${EXTRACT}/qdrant-snapshot.snapshot" >/dev/null \
            || die "Qdrant snapshot upload failed."
        log "Qdrant restore complete."
    else
        log "Skipped Qdrant — you can rebuild embeddings later with scripts/reindex.py."
    fi
    echo
fi

# ── 3. Document files ───────────────────────────────────────────────────
if has_member 'files'; then
    echo "--- Document files ---"
    echo "Syncs restored files into ${DATA_PATH}/documents/files (overwrites by name)."
    echo
    if confirm "Restore document files?"; then
        log "Extracting files..."
        extract_member 'files'
        mkdir -p "${DATA_PATH}/documents/files"
        if command -v rsync >/dev/null 2>&1; then
            rsync -a "${EXTRACT}/files/" "${DATA_PATH}/documents/files/"
        else
            cp -a "${EXTRACT}/files/." "${DATA_PATH}/documents/files/"
        fi
        log "Document files restore complete."
    else
        log "Skipped document files."
    fi
    echo
fi

# ── 4. Auth tokens ──────────────────────────────────────────────────────
if has_member 'auth'; then
    echo "--- Auth tokens ---"
    echo "Syncs restored tokens into ${DATA_PATH}/auth (Google Calendar etc.)."
    echo
    if confirm "Restore auth tokens?"; then
        log "Extracting auth..."
        extract_member 'auth'
        mkdir -p "${DATA_PATH}/auth"
        if command -v rsync >/dev/null 2>&1; then
            rsync -a "${EXTRACT}/auth/" "${DATA_PATH}/auth/"
        else
            cp -a "${EXTRACT}/auth/." "${DATA_PATH}/auth/"
        fi
        log "Auth tokens restore complete."
    else
        log "Skipped auth tokens."
    fi
    echo
fi

# ── Bring the stack back up ─────────────────────────────────────────────
echo "--- Restart ---"
if confirm "Bring the full stack up ('docker compose up -d')?"; then
    "${COMPOSE[@]}" up -d
    log "Waiting for the API to come up..."
    for _ in $(seq 1 30); do
        curl -fsS -o /dev/null "$API_HEALTH_URL" 2>/dev/null && break
        sleep 2
    done
elif [ "${API_WAS_STOPPED:-0}" = "1" ]; then
    echo "Note: the API container was stopped for the PostgreSQL restore — start it with:"
    echo "  ${COMPOSE[*]} up -d"
fi
echo

# ── Verify ──────────────────────────────────────────────────────────────
echo "--- Verify ---"
if curl -fsS -o /dev/null "$API_HEALTH_URL" 2>/dev/null; then
    echo "  API health: OK"
    STATS="$(curl -fsS "$API_STATS_URL" 2>/dev/null || true)"
    [ -n "$STATS" ] && echo "  API stats:  $STATS"
else
    echo "  API health: not reachable at ${API_HEALTH_URL}"
    echo "  (the stack may still be starting, or the API was left stopped)"
fi
echo
log "=== Restore procedure complete ==="
