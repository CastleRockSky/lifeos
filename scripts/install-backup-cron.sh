#!/usr/bin/env bash
# install-backup-cron.sh — Install the nightly LifeOS backup cron + logrotate.
#
# What it does:
#   1. Installs scripts/lifeos-backup.cron → /etc/cron.d/lifeos-backup
#   2. Installs a logrotate config for /var/log/lifeos-backup.log
#   3. Creates the log file with root:adm 0640 perms (logrotate-friendly)
#   4. Runs the backup once as a smoke test
#
# Idempotent: re-running just overwrites the cron/logrotate files.
#
# Usage:
#     sudo scripts/install-backup-cron.sh
#
# To uninstall:
#     sudo rm /etc/cron.d/lifeos-backup /etc/logrotate.d/lifeos-backup

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CRON_SRC="${REPO_DIR}/scripts/lifeos-backup.cron"
BACKUP_SCRIPT="${REPO_DIR}/scripts/backup.sh"

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: must be run as root (use sudo)." >&2
    exit 1
fi

if [ ! -f "$CRON_SRC" ]; then
    echo "ERROR: $CRON_SRC not found." >&2
    exit 1
fi

if [ ! -x "$BACKUP_SCRIPT" ]; then
    echo "ERROR: $BACKUP_SCRIPT not executable." >&2
    exit 1
fi

# Surface Azure env vars from .env (docker-compose .env format — values may
# contain shell metacharacters, so don't `source` it). Reuses the same loader
# the backup script ships with so behavior is identical.
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
load_env "$REPO_DIR/.env"

if [ -n "${AZURE_STORAGE_ACCOUNT:-}" ] && [ -n "${AZURE_CONTAINER:-}" ] && [ -n "${AZURE_SAS_TOKEN:-}" ]; then
    AZURE_CONFIGURED=1
else
    AZURE_CONFIGURED=0
fi

if [ "$AZURE_CONFIGURED" = "1" ] && ! command -v azcopy >/dev/null 2>&1; then
    echo "[0/4] Installing azcopy (Azure backup upload tool)..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl >/dev/null
    TMPDIR_DL="$(mktemp -d)"
    trap 'rm -rf "$TMPDIR_DL"' EXIT
    curl -fsSL "https://aka.ms/downloadazcopy-v10-linux" -o "$TMPDIR_DL/azcopy.tar.gz"
    tar xzf "$TMPDIR_DL/azcopy.tar.gz" -C "$TMPDIR_DL"
    install -m 0755 "$TMPDIR_DL"/azcopy_linux_*/azcopy /usr/local/bin/azcopy
    rm -rf "$TMPDIR_DL"
    trap - EXIT
    echo "       Installed: $(azcopy --version 2>&1 | head -1)"
fi

echo "[1/4] Installing /etc/cron.d/lifeos-backup..."
install -m 0644 -o root -g root "$CRON_SRC" /etc/cron.d/lifeos-backup
# Cron's run-parts ignores files with execute bit or with dots in the name
# in some configurations; the install above uses no extension and 0644.

echo "[2/4] Setting up log rotation..."
cat > /etc/logrotate.d/lifeos-backup <<'EOF'
/var/log/lifeos-backup.log {
    weekly
    rotate 4
    compress
    delaycompress
    missingok
    notifempty
    create 0640 root adm
}
EOF
chmod 0644 /etc/logrotate.d/lifeos-backup

echo "[3/4] Preparing /var/log/lifeos-backup.log..."
touch /var/log/lifeos-backup.log
chown root:adm /var/log/lifeos-backup.log
chmod 0640 /var/log/lifeos-backup.log

echo "[4/4] Smoke test: running the backup once now..."
echo "       (this writes to ${DATA_PATH:-/srv/lifeos}/backups/)"
"$BACKUP_SCRIPT" >> /var/log/lifeos-backup.log 2>&1
echo "       Smoke test complete. Last 6 log lines:"
tail -6 /var/log/lifeos-backup.log | sed 's/^/         | /'

NEXT_RUN="$(date -d 'tomorrow 03:00' '+%a %Y-%m-%d %H:%M %Z')"
LATEST="$(ls -1t ${DATA_PATH:-/srv/lifeos}/backups/lifeos-*.tar.gz 2>/dev/null | head -1 || true)"
LATEST_SIZE="$(du -h "$LATEST" 2>/dev/null | cut -f1 || echo '?')"

if [ "$AZURE_CONFIGURED" = "1" ]; then
    AZURE_LINE="Off-site:    Azure Blob → ${AZURE_STORAGE_ACCOUNT}/${AZURE_CONTAINER}"
else
    AZURE_LINE="Off-site:    not configured (set AZURE_STORAGE_ACCOUNT/AZURE_CONTAINER/AZURE_SAS_TOKEN in .env)"
fi

cat <<EOF

==============================================
LifeOS backup cron is installed.
==============================================
Schedule:    03:00 local time, every night
Next run:    $NEXT_RUN
Cron file:   /etc/cron.d/lifeos-backup
Log file:    /var/log/lifeos-backup.log (weekly rotation, 4 kept)
Backup dir:  ${DATA_PATH:-/srv/lifeos}/backups/
Retention:   14 most recent tarballs (override with RETAIN_COUNT)
$AZURE_LINE

Most recent backup (from the smoke test):
  $LATEST ($LATEST_SIZE)

To watch the next live run:
  sudo tail -f /var/log/lifeos-backup.log

To trigger an out-of-band backup right now:
  sudo $BACKUP_SCRIPT

To uninstall:
  sudo rm /etc/cron.d/lifeos-backup /etc/logrotate.d/lifeos-backup
==============================================
EOF
