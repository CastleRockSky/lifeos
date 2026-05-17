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

## Retention (grandfather-father-son)

After each run, `backup.sh` prunes old tarballs on a tiered
**grandfather-father-son (GFS)** schedule rather than a flat count. It
keeps the **newest** backup in each of:

| Tier    | Window  | Env var          | Default |
|---------|---------|------------------|---------|
| Daily   | last N days       | `RETAIN_DAILY`   | 7       |
| Weekly  | last N ISO weeks  | `RETAIN_WEEKLY`  | 8       |
| Monthly | last N months     | `RETAIN_MONTHLY` | 12      |

The three keep sets are unioned, so one tarball can satisfy several
tiers (today's backup is simultaneously the current daily, weekly and
monthly). With the defaults the steady state is ~25–27 tarballs: a
week of dailies, ~2 months of weeklies, ~1 year of monthlies.

Windows are counted by the periods **present** in the backup set, not
by the calendar — a skipped night doesn't shrink the window. Override
any tier by adding the env var to `.env`, e.g. `RETAIN_MONTHLY=24`.

The **same GFS rule is applied to the off-site copies** — see each
off-site section below. Note the first run after upgrading from the
old flat 14-tarball scheme will prune a few mid-week backups that fall
outside every tier; this is expected.

## Off-site to Azure Blob (optional)

### One-time setup

1. **Generate a SAS token** in the Azure portal:
   - Storage account → Containers → your container → **Generate SAS**
   - Permissions: **Read, Add, Create, Write, List, Delete**
     (Delete is needed so the nightly GFS prune can expire old blobs)
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

After each upload the script applies the **same GFS schedule** (see
[Retention](#retention-grandfather-father-son) above) to the Azure
container: it lists the blobs, computes the keep set, and `azcopy
remove`s the rest. This needs **Delete** permission on the SAS token
(see step 1). Prune failures are non-fatal — they're logged and the
blob is left in place for the next run to retry.

No Azure lifecycle management rule is required. If you'd rather let
Azure expire blobs instead, you can still add one (Storage account →
Lifecycle management) — but with the script pruning, it's redundant.

## Off-site to Backblaze B2 (optional)

This leg is independent of the Azure one — set it up instead of, or
alongside, Azure. Upload uses [rclone](https://rclone.org)'s native B2
backend.

### One-time setup

1. **Create a bucket** in the Backblaze B2 console (private; any region).

2. **Create a scoped Application Key:**
   - Backblaze account → **Application Keys** → **Add a New Application Key**
   - **Allow access to:** the backup bucket only
   - **Capabilities:** `listBuckets`, `listFiles`, `readFiles`,
     `writeFiles`, `deleteFiles` (`deleteFiles` lets the nightly GFS
     prune expire old objects)
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

After each upload the script applies the **same GFS schedule** (see
[Retention](#retention-grandfather-father-son) above) to the bucket:
it lists the objects, computes the keep set, and deletes the rest with
`rclone deletefile --b2-hard-delete`. This needs the `deleteFiles`
capability on the application key (see step 2). Prune failures are
non-fatal — logged, and the object is left for the next run to retry.

`--b2-hard-delete` makes the prune a real delete that reclaims storage
(B2's default is a "hide" that keeps the object as a prior version).
No bucket Lifecycle Setting is required.

## Failure alerting (HetrixTools heartbeat)

Logging a failure to `/var/log/lifeos-backup.log` only helps if someone
reads the log. A **heartbeat monitor** is a dead-man's-switch: the
backup pings a URL on success, and the monitoring service alerts you
when an expected ping doesn't arrive. Because the alert is triggered by
*absence*, it catches all three failure modes — a crashed run, a run
that fails partway, and a run that **never happens at all** (e.g. a
broken cron entry, which is exactly what bit this project once).

### One-time setup

1. In HetrixTools: **Monitors → Add Monitor → Heartbeat** (a.k.a. cron
   monitor).
2. Set the expected interval to **1 day** with a few hours of grace —
   the backup runs at 03:00, so an alert window of ~26–30h is sensible.
3. Pick your **notification contacts** (email, mobile push, Telegram,
   Slack, webhook — whatever you already use in HetrixTools).
4. Copy the monitor's **ping URL** and add it to `.env`:

   ```
   HETRIX_HEARTBEAT_URL=https://hetrixtools.com/heartbeat/<token>/
   ```

That's it — no installer re-run needed; `backup.sh` reads `.env` on
every run.

### What gets pinged

`backup.sh` pings the URL **only on a clean run**, as its last step.
`set -e` aborts the script before that point on any core failure
(Postgres dump, Qdrant snapshot, archive write), so a failed or
crashed run simply sends no ping.

Off-site upload failures (Azure / B2) are **non-fatal** and do *not*
suppress the heartbeat — they stay visible in the log and on the
Settings page. If you want those alerted on too, add a second heartbeat
monitor and a ping inside the upload blocks.

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

A backup is a single tarball with four pieces (PostgreSQL dump, Qdrant
snapshot, document files, auth tokens). `scripts/restore.sh` restores
any subset of them interactively.

```bash
sudo scripts/restore.sh                       # pick from local backups (or pull from B2)
sudo scripts/restore.sh /path/to/lifeos-….tar.gz   # restore a specific tarball
```

The script:

1. **Selects a backup** — with no argument it lists the tarballs in
   `${DATA_PATH}/backups/` to choose from, and (when B2 is configured
   in `.env`) offers a `[b]` option to pull the newest off-site copy.
2. **Confirms each component separately** — PostgreSQL, Qdrant,
   document files, auth tokens. Skipping any of them is safe, so you
   can do a partial restore (e.g. just the database).
3. **PostgreSQL** — stops the API container, then `DROP DATABASE …
   WITH (FORCE)` + `CREATE DATABASE` + loads the dump in a single
   transaction (`ON_ERROR_STOP`, so a bad dump rolls back cleanly).
4. **Qdrant** — uploads the snapshot to the collection's
   `snapshots/upload` endpoint (`priority=snapshot`).
5. **File trees** — `rsync` (or `cp -a`) the `files/` and `auth/`
   trees back into `${DATA_PATH}`.
6. **Restarts** the stack and verifies `/api/health` + `/api/stats`.

It must run as root (it writes under `/srv/lifeos` and drives docker)
and reads `.env` for the database/Qdrant/B2 settings.

### Full disaster recovery onto a fresh host

```bash
git clone https://github.com/CastleRockSky/lifeos.git /opt/lifeos
cd /opt/lifeos
sudo mkdir -p /srv/lifeos/{postgres,qdrant,documents/files,documents/inbox,auth,backups}
cp .env.example .env
# Fill in POSTGRES_PASSWORD, ANTHROPIC_API_KEY, SECRET_KEY, and the B2 vars

sudo docker compose up -d        # bring the (empty) stack up
sudo scripts/restore.sh          # choose [b] to pull the newest backup from B2
```

If anything looks off, the source tarball is untouched — nothing the
restore does is irreversible without the original data.

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
