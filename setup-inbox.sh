#!/bin/bash
# ============================================================================
# LifeOS Inbox Samba Setup
# Run with: sudo bash ~/lifeos/setup-inbox.sh
# ============================================================================
set -euo pipefail

INBOX_DIR="/srv/lifeos/documents/inbox"

echo "==> Fixing inbox permissions..."
chmod 777 "${INBOX_DIR}" "${INBOX_DIR}/processed" "${INBOX_DIR}/failed"

echo "==> Adding Samba share..."
if grep -q '\[LifeOS Scan\]' /etc/samba/smb.conf 2>/dev/null; then
    echo "    Share already exists — skipping."
else
    cat >> /etc/samba/smb.conf <<'EOF'

[LifeOS Scan]
   path = /srv/lifeos/documents/inbox
   browseable = yes
   writable = yes
   guest ok = yes
   force user = root
   force group = root
   create mask = 0666
   directory mask = 0777
   comment = LifeOS document inbox
EOF
    echo "    Share added."
fi

echo "==> Restarting Samba..."
systemctl restart smbd nmbd

echo ""
echo "Done! Share available at: \\\\$(hostname -I | awk '{print $1}')\\LifeOS Scan"
