#!/usr/bin/env bash
# setup-scanner-share.sh — Stand up an SMB share pointing at the LifeOS inbox.
#
# After running this, configure your scanner to "Scan to Network Folder /
# Scan to SMB" with the credentials printed at the end. Files land in the
# inbox folder that the LifeOS inbox_watcher already polls — they get
# ingested within INBOX_POLL_INTERVAL seconds (default 10).
#
# Re-runnable: idempotent. Re-running rotates the samba password if you set
# SHARE_PASSWORD, otherwise re-generates one.
#
# Usage:
#     sudo scripts/setup-scanner-share.sh
#
# Optional env vars:
#     SHARE_NAME       (default: lifeos-inbox)
#     SHARE_USER       (default: scanner)
#     SHARE_PASSWORD   (default: random 20-char string)
#     INBOX_DIR        (default: ${DATA_PATH:-/srv/lifeos}/documents/inbox)
#     SHARE_GUEST=1    (allow anonymous writes — some older scanners need this)

set -euo pipefail

SHARE_NAME="${SHARE_NAME:-lifeos-inbox}"
SHARE_USER="${SHARE_USER:-scanner}"
INBOX_DIR="${INBOX_DIR:-${DATA_PATH:-/srv/lifeos}/documents/inbox}"
SHARE_PASSWORD="${SHARE_PASSWORD:-}"
SHARE_GUEST="${SHARE_GUEST:-0}"

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: must be run as root (use sudo)." >&2
    exit 1
fi

echo "[1/8] Installing samba and avahi-daemon..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    samba samba-common-bin avahi-daemon >/dev/null

echo "[2/8] Ensuring system user '$SHARE_USER' exists..."
if ! id -u "$SHARE_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SHARE_USER"
    echo "       Created user $SHARE_USER"
else
    echo "       User $SHARE_USER already exists"
fi

echo "[3/8] Preparing inbox directory $INBOX_DIR..."
mkdir -p "$INBOX_DIR"
chown "$SHARE_USER":"$SHARE_USER" "$INBOX_DIR"
chmod 0775 "$INBOX_DIR"

echo "[4/8] Setting samba password..."
if [ -z "$SHARE_PASSWORD" ]; then
    SHARE_PASSWORD="$(openssl rand -base64 18 | tr -d '/+=' | head -c 20)"
    GENERATED_PASSWORD=1
else
    GENERATED_PASSWORD=0
fi
( echo "$SHARE_PASSWORD"; echo "$SHARE_PASSWORD" ) | smbpasswd -as "$SHARE_USER" >/dev/null

echo "[5/8] Writing share definition to /etc/samba/smb.conf..."
SMB_CONF=/etc/samba/smb.conf
MARK_BEGIN="# BEGIN LifeOS Scanner Share"
MARK_END="# END LifeOS Scanner Share"

# Backup once
[ -f "${SMB_CONF}.lifeos.bak" ] || cp -a "$SMB_CONF" "${SMB_CONF}.lifeos.bak"

# Remove existing block if present (idempotent)
if grep -q "$MARK_BEGIN" "$SMB_CONF"; then
    sed -i "/$MARK_BEGIN/,/$MARK_END/d" "$SMB_CONF"
fi

if [ "$SHARE_GUEST" = "1" ]; then
    AUTH_BLOCK="    guest ok = yes
    guest only = yes
    map to guest = Bad User"
else
    AUTH_BLOCK="    valid users = $SHARE_USER
    guest ok = no"
fi

cat >> "$SMB_CONF" <<EOF

$MARK_BEGIN
[$SHARE_NAME]
    comment = LifeOS document inbox — drop scans here
    path = $INBOX_DIR
    browseable = yes
    read only = no
    create mask = 0664
    directory mask = 0775
$AUTH_BLOCK
    force user = $SHARE_USER
    force group = $SHARE_USER
    # macOS / iOS Files compatibility
    vfs objects = catia fruit streams_xattr
    fruit:metadata = stream
    fruit:resource = stream
    fruit:posix_rename = yes
    # Modern protocol baseline
    server min protocol = SMB2
$MARK_END
EOF

echo "[6/8] Validating smb.conf..."
if ! testparm -s "$SMB_CONF" >/dev/null 2>&1; then
    echo "       smb.conf failed validation; restoring backup." >&2
    cp -a "${SMB_CONF}.lifeos.bak" "$SMB_CONF"
    exit 1
fi

echo "[7/8] Restarting samba and avahi..."
systemctl enable smbd nmbd avahi-daemon >/dev/null 2>&1 || true
systemctl restart smbd nmbd avahi-daemon

echo "[8/8] Done."
echo

HOSTNAME_SHORT="$(hostname -s)"
IP4="$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}' || hostname -I | awk '{print $1}')"

cat <<EOF
==============================================
LifeOS Scanner share is up.
==============================================
Share path (mDNS):  smb://${HOSTNAME_SHORT}.local/${SHARE_NAME}
Share path (IP):    smb://${IP4}/${SHARE_NAME}
Inbox folder:       ${INBOX_DIR}

EOF

if [ "$SHARE_GUEST" = "1" ]; then
    cat <<EOF
Authentication:     GUEST (anonymous, no password)
==============================================

Configure your scanner's "Scan to Network Folder" / "SMB" target with the
share path above. No username or password required.

Note: guest mode is convenient but anyone on your LAN can write here.
For tighter security, re-run without SHARE_GUEST=1.
EOF
else
    cat <<EOF
Username:           ${SHARE_USER}
Password:           ${SHARE_PASSWORD}
==============================================

Configure your scanner's "Scan to Network Folder" / "SMB" target with the
share path and credentials above.

EOF
    if [ "$GENERATED_PASSWORD" = "1" ]; then
        cat <<EOF
This password was generated for you. Save it now — it won't be displayed
again. To rotate, set SHARE_PASSWORD=newpassword and re-run this script.
EOF
    fi
fi

cat <<EOF

Files dropped in the share will be ingested by LifeOS within ~10 seconds.
Watch progress in: docker logs -f lifeos-api
For brand-specific scanner setup tips, see: docs/SCANNER-SETUP.md
EOF
