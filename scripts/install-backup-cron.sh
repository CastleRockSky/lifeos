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
