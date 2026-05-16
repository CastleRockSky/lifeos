# LifeOS Backup & Restore

Nightly backup of the full LifeOS state (PostgreSQL, Qdrant, document
files, auth tokens) bundled into a single timestamped tarball.
Optional off-site mirror to Azure Blob Storage and/or Backblaze B2 —
the two legs are independent, so you can run either, both, or neither.

---

## What gets backed up

| Component | How |
|---|---|
| PostgreSQL | `pg_dump` of the `lifeos` database via `docker exec` |
| Qdrant     | HTTP snapshot API → downloaded as a `.snapshot` file |
| Document files | `cp -a` from `${DATA_PATH}/documents/files/` |
| Auth tokens (Google Calendar etc.) | `cp -a` from `${DATA_PATH}/auth/` |

Everything ends up in a single `lifeos-YYYYMMDD-HHMMSS.tar.gz` under
`${DATA_PATH}/backups/` with `chmod 0600`.

## Install (one time)

```bash
sudo scripts/install-backup-cron.sh
```

This drops `/etc/cron.d/lifeos-backup` (03:00 nightly), sets up
logrotate, creates the log file, runs a smoke test, and prints next-run
info. Re-running just refreshes the cron + logrotate configs.

To uninstall:

```bash
sudo rm /etc/cron.d/lifeos-backup /etc/logrotate.d/lifeos-backup
```

## Off-site to Azure Blob (optional)

### One-time setup

1. **Generate a SAS token** in the Azure portal:
   - Storage account → Containers → your container → **Generate SAS**
   - Permissions: **Read, Add, Create, Write, List**
   - Expiry: pick something reasonable (1 year is fine)
   - Allowed protocols: **HTTPS only**
   - Click **Generate SAS token and URL**, copy the **SAS token** (the
     query string starting with `sv=`)

2. **Add the three values to `.env`** (in the repo):

   ```
   AZURE_STORAGE_ACCOUNT=ezekielbackupscrs
   AZURE_CONTAINER=lifeos-db
   AZURE_SAS_TOKEN=sv=...&sig=...
   # Optional: pin to a subfolder inside the container
   # AZURE_BLOB_PREFIX=nightly
   ```

3. **Re-run the installer** — it'll detect the Azure env, download
   azcopy from Microsoft, and run a smoke test that uploads to your
   container:

   ```bash
   sudo scripts/install-backup-cron.sh
   ```

4. **Verify** the blob landed:

   ```bash
   ls -lt ${DATA_PATH:-/srv/lifeos}/backups/   # newest local file
   # In Azure portal → Containers → lifeos-db, refresh — same filename
   ```

### What happens nightly

After the tarball is written locally, the script uploads exactly one
new blob to Azure (no incremental diffing — full nightly tarballs).
Failures are **non-fatal**: the local backup is always kept, and the
azcopy error is logged to `/var/log/lifeos-backup.log` so the next
night can retry without intervention.

### Remote retention

The script doesn't delete old Azure blobs. Configure an **Azure
lifecycle management** rule on the storage account if you want to
auto-expire:

- Storage account → Lifecycle management → Add rule
- Scope: blobs in `lifeos-db`
- Action: delete blobs **older than N days**

A common pattern: keep 30 days of dailies, then transition to cool
storage for 90 days, then delete.

## Off-site to Backblaze B2 (optional)

This leg is independent of the Azure one — set it up instead of, or
alongside, Azure. Upload uses [rclone](https://rclone.org)'s native B2
backend.

### One-time setup

1. **Create a bucket** in the Backblaze B2 console (private; any region).

2. **Create a scoped Application Key:**
   - Backblaze account → **Application Keys** → **Add a New Application Key**
   - **Allow access to:** the backup bucket only
   - **Capabilities:** `listBuckets`, `listFiles`, `readFiles`, `writeFiles`
   - Copy the **keyID** and **applicationKey** — the key is shown only once.

3. **Add the values to `.env`** (in the repo):

   ```
   B2_BUCKET=lifeos-backups
   B2_KEY_ID=0011223344556677889900aa
   B2_APPLICATION_KEY=K001xxxxxxxxxxxxxxxxxxxxxxxxxxx
   # Optional: pin to a subfolder inside the bucket
   # B2_PREFIX=nightly
   ```

4. **Re-run the installer** — it detects the B2 env, installs `rclone`
   from apt, and the smoke test uploads to your bucket:

   ```bash
   sudo scripts/install-backup-cron.sh
   ```

5. **Verify** the object landed — in the B2 console, browse the bucket;
   the newest file matches the local tarball name.

### What happens nightly

After the local tarball is written, the script runs `rclone copy` to
push exactly that one file to B2 (full nightly tarballs, no diffing).
Like the Azure leg, failures are **non-fatal**: the local backup is
kept and the rclone error is logged to `/var/log/lifeos-backup.log`.

### Remote retention

The script doesn't delete old B2 objects. Set a **Lifecycle Setting**
on the bucket (Backblaze console → bucket → Lifecycle Settings) to
auto-expire — e.g. "keep only the last version, hide/delete after N
days."

## Operational commands

```bash
# Tail tonight's run live
sudo tail -f /var/log/lifeos-backup.log

# Trigger an out-of-band backup right now (also uploads if Azure is configured)
sudo scripts/backup.sh

# Trigger without the Azure leg (useful for debugging)
sudo AZURE_STORAGE_ACCOUNT= scripts/backup.sh

# List local backups
ls -lh /srv/lifeos/backups/

# Look at what's inside the most recent tarball without extracting
tar -tzvf "$(ls -1t /srv/lifeos/backups/lifeos-*.tar.gz | head -1)" | head -20
```

## Restore

Backup is a single tarball with four pieces. Restore is manual — these
are the steps for a full disaster recovery onto a fresh host.

### 1. Pre-flight

```bash
# On the new host
git clone https://github.com/CastleRockSky/lifeos.git /opt/lifeos
cd /opt/lifeos
sudo mkdir -p /srv/lifeos/{postgres,qdrant,documents/files,documents/inbox,auth,backups}
cp .env.example .env
# Fill in POSTGRES_PASSWORD, ANTHROPIC_API_KEY, SECRET_KEY, plus Azure vars if you want
```

### 2. Bring up the stack (empty)

```bash
sudo docker compose up -d
# Wait for healthchecks
sudo docker compose ps
```

### 3. Pull the most recent backup off-site (if local copies are gone)

**From Azure Blob:**

```bash
# Quick one-liner using azcopy (auto-installed by install-backup-cron.sh).
# Lists newest 5 blobs:
azcopy list "https://ezekielbackupscrs.blob.core.windows.net/lifeos-db?${AZURE_SAS_TOKEN}" \
    | sort -k4 -r | head -5

# Download the one you want:
azcopy copy \
    "https://ezekielbackupscrs.blob.core.windows.net/lifeos-db/lifeos-20260512-090000.tar.gz?${AZURE_SAS_TOKEN}" \
    /tmp/restore.tar.gz
```

**From Backblaze B2** (rclone, creds from `.env`):

```bash
# List newest files in the bucket:
rclone lsl ":b2:${B2_BUCKET}/${B2_PREFIX}" \
    --b2-account="$B2_KEY_ID" --b2-key="$B2_APPLICATION_KEY" \
    --config /dev/null | sort -k2 -r | head -5

# Download the one you want:
rclone copy ":b2:${B2_BUCKET}/${B2_PREFIX}/lifeos-20260512-090000.tar.gz" /tmp/ \
    --b2-account="$B2_KEY_ID" --b2-key="$B2_APPLICATION_KEY" --config /dev/null
mv /tmp/lifeos-20260512-090000.tar.gz /tmp/restore.tar.gz
```

### 4. Extract

```bash
mkdir -p /tmp/restore && tar -xzf /tmp/restore.tar.gz -C /tmp/restore
ls /tmp/restore/   # expect: auth/  files/  postgres.sql  qdrant-snapshot.snapshot
```

### 5. Restore PostgreSQL

```bash
# Drop and recreate the schema (the dump includes CREATE TABLE statements)
sudo docker exec -i lifeos-postgres psql -U lifeos -d postgres -c "DROP DATABASE IF EXISTS lifeos;"
sudo docker exec -i lifeos-postgres psql -U lifeos -d postgres -c "CREATE DATABASE lifeos;"
sudo docker exec -i lifeos-postgres psql -U lifeos -d lifeos < /tmp/restore/postgres.sql
```

### 6. Restore Qdrant

```bash
# Stop API so it doesn't reconnect mid-restore
sudo docker compose stop api

# Upload the snapshot back to Qdrant via the recover endpoint
curl -X PUT "http://localhost:6334/collections/documents/snapshots/recover" \
    -H "Content-Type: application/json" \
    -d "{\"location\": \"file:///tmp/qdrant-snapshot.snapshot\"}"
# (Or: docker cp the .snapshot into the qdrant container's snapshots dir first.)

sudo docker compose start api
```

### 7. Restore file trees

```bash
sudo cp -a /tmp/restore/files/. /srv/lifeos/documents/files/
sudo cp -a /tmp/restore/auth/.  /srv/lifeos/auth/
sudo chown -R root:root /srv/lifeos/auth
```

### 8. Verify

```bash
curl http://localhost:8000/api/health
curl http://localhost:8000/api/stats   # documents count should match pre-restore
```

If anything looks off, the original tarball is still in `/tmp/` —
nothing the restore does is irreversible without source data.

## Security notes

- The SAS token grants write access to one container. **Don't share it
  or commit it.** `.env` is in `.gitignore` for this reason.
- Set the SAS expiry to something you can rotate on a known cadence
  (e.g. 1 year). Re-running `install-backup-cron.sh` after updating
  the token in `.env` picks it up — no other action needed.
- Use a SAS scoped to the **container**, not the storage account. If
  it leaks, the blast radius is one container's worth of nightly
  tarballs.
- For extra protection, set **Allowed IP addresses** on the SAS to
  your home/NUC public IP.
