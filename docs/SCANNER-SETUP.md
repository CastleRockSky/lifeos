# Scan-to-LifeOS

Configure a network scanner (or a phone/computer) to drop documents
directly into LifeOS via SMB. Files written to the share land in the
inbox folder that the API server polls every 10 seconds, so anything
you scan is ingested, OCR'd, and AI-classified within a minute or two.

---

## Server setup (one time, on the host)

```bash
sudo scripts/setup-scanner-share.sh
```

Save the printed password somewhere safe (1Password, sticky note, etc.)
— it's generated once and not stored anywhere recoverable.

Defaults the script picks:

| Setting | Default | Override with |
|---|---|---|
| Share name | `lifeos-inbox` | `SHARE_NAME=` |
| Username | `scanner` | `SHARE_USER=` |
| Password | random 20-char | `SHARE_PASSWORD=` |
| Path on disk | `${DATA_PATH:-/srv/lifeos}/documents/inbox` | `INBOX_DIR=` |

Need anonymous (no-password) mode for an old scanner that can't auth?
Re-run with `SHARE_GUEST=1`. **Only do this on a trusted LAN** — anyone
on the network can write to it.

---

## Pointing a scanner at it

Most scanners call this **Scan to SMB**, **Scan to Network Folder**, or
**Scan to Shared Folder**. Setup happens in the scanner's web admin UI
(open the printer's IP in a browser) or its front-panel menu.

You'll need three pieces from the script's output:

- **Server / hostname:** `lifeos.local` (mDNS) or the IPv4 address
- **Share name:** `lifeos-inbox` (the part after the last `/` in the SMB URL)
- **Username + password:** as printed by the script

### Brother (most ADF / DCP / MFC models)

1. Open the Brother web UI (`http://<printer-ip>/`).
2. **Scan → Scan to Network Profile → Profile 1**.
3. Set:
   - **Network Folder Path:** `\\lifeos.local\lifeos-inbox`
     (or `\\<ip>\lifeos-inbox` if mDNS isn't working)
   - **Auth Method:** `Auto`
   - **Username:** `scanner`
   - **Password:** the printed password
   - **File Type:** `PDF` (preferred — multi-page works)
   - **File Name:** leave Brother's default
4. Save. On the printer's front panel: **Scan → Network → Profile 1**.

### Canon (imageRUNNER, MAXIFY, PIXMA Pro)

1. Open the Canon web UI (Remote UI).
2. **Address Book → Register New Destination → File**.
3. Set:
   - **Protocol:** `Windows (SMB)`
   - **Hostname:** `\\lifeos.local`
   - **Folder Path:** `lifeos-inbox`
   - **Username / Password:** as printed
4. From the front panel: **Scan → Send → Address Book → LifeOS**.

### HP (LaserJet Pro / OfficeJet Pro)

1. Open the HP Embedded Web Server.
2. **Scan → Scan to Network Folder → Setup**.
3. Add a Quick Set:
   - **Network Folder Path:** `\\lifeos.local\lifeos-inbox`
   - **Authentication:** Use the credentials below
   - **Username / Password:** as printed
4. From the front panel: **Scan → Network Folder → LifeOS Quick Set**.

### Epson (WorkForce, EcoTank)

1. Open the Epson web UI (Web Config).
2. **Scan/Copy → Network Folder/FTP**.
3. Add:
   - **Communication Mode:** SMB
   - **Save to:** `\\lifeos.local\lifeos-inbox`
   - **User Name / Password:** as printed
4. From the front panel: **Scan → Network Folder/FTP → LifeOS**.

### macOS (drag-and-drop)

In Finder: **Go → Connect to Server** (`Cmd+K`).
Enter `smb://lifeos.local/lifeos-inbox`. When prompted, use the
`scanner` username and printed password. The share appears in the
sidebar; drag any file in to ingest it.

### iOS / iPadOS (Files app)

Files → … → **Connect to Server** → `smb://lifeos.local/lifeos-inbox`.
Same credentials. Useful for sharing a scanned PDF directly from a
scanning app (Adobe Scan, Genius Scan, the built-in Notes scanner).

### Windows (drag-and-drop or scanner)

In Explorer address bar: `\\lifeos.local\lifeos-inbox`. Authenticate
with the `scanner` credentials.

---

## What happens after a file lands

1. The inbox watcher (running inside the `lifeos-api` container) picks
   up the file once its size is stable for `INBOX_STABILITY_SECONDS`
   (default 5).
2. The file is moved through the standard pipeline:
   text/OCR extraction → embedding → AI analysis → classification.
3. On success, the file moves from `inbox/` to `inbox/processed/`.
   On failure, it moves to `inbox/failed/` and the error is logged.
4. The new document appears in the LifeOS web UI under
   **Recent Documents** (and any matching domain dashboard).

Watch progress live:

```bash
docker logs -f lifeos-api | grep -i 'inbox\|ai analysis'
```

---

## Troubleshooting

**Scanner can't find the server.** Use the IP address instead of the
mDNS hostname. If you also need NetBIOS name resolution (older
scanners), the `nmbd` service set up by the script handles that.

**"Permission denied" from the scanner.** Re-run the script — it
rotates the samba password each time unless you pin one with
`SHARE_PASSWORD=`. Make sure the scanner uses *exactly* the printed
password (some scanners strip special characters; the script avoids
characters most likely to cause problems).

**Files land but don't get ingested.** Check the watcher is enabled:

```bash
curl http://localhost:8000/api/inbox/status
```

If it reports `"enabled": false`, set `INBOX_ENABLED=true` in `.env`
and `docker compose restart api`.

**File got moved to `inbox/failed/`.** Open the file from there and
inspect — usually corrupt PDF, password-protected, or an unsupported
format. The watcher logs the underlying error in the container log.

**Want to disable the share temporarily.** Stop and disable samba:

```bash
sudo systemctl stop smbd nmbd
sudo systemctl disable smbd nmbd
```

The setup script can re-enable everything by re-running.
