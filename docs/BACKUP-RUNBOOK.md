# LifeOS Backup & Recovery Runbook (portable copy)

> **Keep this somewhere that survives the NUC dying** — 1Password, Google
> Drive, a printout in a drawer. It is a self-contained summary of how
> LifeOS is backed up and how to restore from nothing. The live, detailed
> version lives in the repo at `docs/BACKUP-SETUP.md`; this copy exists so
> you can recover even if the repo and the machine are both gone.
>
> _Last updated: 2026-05-31. Owner: Dave (Castle Rock, CO)._

---

## The one-paragraph version

Every night at **03:00 local**, a cron job runs `scripts/backup.sh` on the
NUC. It dumps PostgreSQL, snapshots Qdrant, and copies the document files and
auth tokens into one tarball: `/srv/lifeos/backups/lifeos-YYYYMMDD-HHMMSS.tar.gz`.
That tarball is then uploaded **off-site to Backblaze B2**. Old copies (local
and remote) are pruned on a grandfather-father-son schedule. A HetrixTools
heartbeat alerts you if a night is ever missed. To recover, you run
`scripts/restore.sh`, which can pull the newest tarball straight from B2.

---

## What's in a backup

A single `lifeos-YYYYMMDD-HHMMSS.tar.gz` contains four pieces:

| Component | Source | How |
|---|---|---|
| PostgreSQL | `lifeos` database | `pg_dump` via `docker exec lifeos-postgres` |
| Qdrant vectors | `documents` collection | HTTP snapshot API → `.snapshot` file |
| Document files | `/srv/lifeos/documents/files/` | `cp -a` |
| Auth tokens | `/srv/lifeos/auth/` (Google Calendar etc.) | `cp -a` |

Written with `chmod 0600` to `/srv/lifeos/backups/`. Qdrant can be rebuilt from
Postgres + files via `scripts/reindex.py` if a snapshot is ever missing, so the
database and document files are the irreplaceable parts.

---

## Where everything lives

| Thing | Location |
|---|---|
| Backup script | `scripts/backup.sh` (in the repo, deployed to the NUC) |
| Restore script | `scripts/restore.sh` |
| Installer | `scripts/install-backup-cron.sh` |
| Cron entry | `/etc/cron.d/lifeos-backup` (runs as root, 03:00 nightly) |
| Log | `/var/log/lifeos-backup.log` (weekly rotation, 4 kept) |
| Local backups | `/srv/lifeos/backups/lifeos-*.tar.gz` |
| All persistent data | `/srv/lifeos/` (postgres, qdrant, documents, auth, backups) |
| Config / secrets | `.env` in the repo root (NOT committed — in `.gitignore`) |
| Repo | `https://github.com/CastleRockSky/lifeos.git` (deployed to `/opt/lifeos` on a fresh host) |

---

## Secrets you must have to recover (store these alongside this doc)

These live in `.env` on the NUC. If the NUC is gone, you need them from your
password manager — **back them up there now if you haven't.**

- `POSTGRES_PASSWORD` — database password
- `ANTHROPIC_API_KEY` — Claude API key
- `SECRET_KEY` — app secret
- `B2_BUCKET`, `B2_KEY_ID`, `B2_APPLICATION_KEY` — Backblaze off-site backup
  (the key is shown only once at creation — if lost, make a new Application Key)
- `HETRIX_HEARTBEAT_URL` — failure-alert heartbeat ping URL

> The off-site backup is **only as recoverable as these credentials.** A
> tarball sitting safely in B2 is useless if you can't authenticate to pull it.

---

## Off-site: Backblaze B2

The active off-site target. (Azure Blob support also exists in the script but
was disabled 2026-05-16 — B2 is the sole off-site copy now.)

- Upload uses `rclone`'s native B2 backend (creds passed as flags, no
  `rclone.conf`). Installed by the installer.
- The B2 Application Key needs: `listBuckets`, `listFiles`, `readFiles`,
  `writeFiles`, `deleteFiles`. (`deleteFiles` lets the nightly prune expire old
  objects with a real `--b2-hard-delete`, not B2's default "hide".)
- Upload failures are **non-fatal** — the local backup is always kept and the
  error is logged for the next night to retry.

---

## Retention (grandfather-father-son)

After each run the script keeps the **newest** backup in each tier and prunes
the rest — applied identically to local disk and to B2:

| Tier | Window | Env var | Default |
|---|---|---|---|
| Daily | last N days | `RETAIN_DAILY` | 7 |
| Weekly | last N ISO weeks | `RETAIN_WEEKLY` | 8 |
| Monthly | last N months | `RETAIN_MONTHLY` | 12 |

Tiers are unioned (today's backup counts as the current daily, weekly *and*
monthly), so steady state is ~25–27 tarballs: a week of dailies, ~2 months of
weeklies, ~1 year of monthlies. Windows are counted by what's **present** in
the backup set, not the calendar — a skipped night doesn't shrink the window.

---

## Failure alerting (HetrixTools heartbeat)

`backup.sh` pings `HETRIX_HEARTBEAT_URL` **only on a clean run, as its last
step**. Because `set -e` aborts before that on any core failure, a crashed,
failed, or **never-ran** backup simply sends no ping — and HetrixTools alerts
you once the expected window (~26–30h, since it runs at 03:00) lapses. This is
a dead-man's-switch; it catches the "broken cron, no backups for weeks" failure
mode that a success-only notification would miss. (Off-site upload failures are
non-fatal and do *not* suppress the heartbeat — watch the log for those.)

---

## Everyday operations

```bash
# Watch tonight's run live
sudo tail -f /var/log/lifeos-backup.log

# Run a backup right now (out of band)
sudo /opt/lifeos/scripts/backup.sh

# List local backups
ls -lh /srv/lifeos/backups/

# Peek inside the newest tarball without extracting
tar -tzvf "$(ls -1t /srv/lifeos/backups/lifeos-*.tar.gz | head -1)" | head -20

# (Re)install / refresh the cron + logrotate (idempotent; runs a smoke test)
sudo /opt/lifeos/scripts/install-backup-cron.sh
```

---

## Restore (existing, working host)

```bash
sudo scripts/restore.sh                              # pick from local, or [b] = pull newest from B2
sudo scripts/restore.sh /path/to/lifeos-….tar.gz     # restore a specific tarball
```

The script is interactive and confirms **each component separately**, so a
partial restore (e.g. just the database) is safe. For each:

1. **Select** a tarball — local list, or `[b]` to download the newest from B2.
2. **PostgreSQL** — stops the API container, `DROP DATABASE … WITH (FORCE)` +
   `CREATE DATABASE` + loads the dump in a single transaction (`ON_ERROR_STOP`,
   so a bad dump rolls back cleanly).
3. **Qdrant** — uploads the snapshot to the collection's `snapshots/upload`.
4. **Files / auth** — `rsync` (or `cp -a`) the trees back into `/srv/lifeos`.
5. **Restart** the stack and verify `/api/health` + `/api/stats`.

Runs as root (writes under `/srv/lifeos`, drives docker) and reads `.env`.
The source tarball is never modified — nothing it does is irreversible while
you still hold the original data.

---

## Full disaster recovery onto a fresh machine

```bash
git clone https://github.com/CastleRockSky/lifeos.git /opt/lifeos
cd /opt/lifeos
sudo mkdir -p /srv/lifeos/{postgres,qdrant,documents/files,documents/inbox,auth,backups}
cp .env.example .env
# Fill in from your password manager:
#   POSTGRES_PASSWORD, ANTHROPIC_API_KEY, SECRET_KEY,
#   B2_BUCKET, B2_KEY_ID, B2_APPLICATION_KEY  (and HETRIX_HEARTBEAT_URL)

sudo docker compose up -d        # bring the empty stack up
sudo scripts/restore.sh          # choose [b] to pull the newest backup from B2
```

Then re-arm nightly backups on the new host:

```bash
sudo scripts/install-backup-cron.sh
```

> **Port note:** LifeOS is shifted off the default ports because the NUC also
> runs the legacy Ezekiel stack. API is `:8100`, Qdrant `:6334`, Postgres
> `:5433`, Nginx `:8180` on the host. Inside containers the ports are
> unchanged.

---

## Recovery readiness checklist

- [ ] `.env` secrets (above) are stored in your password manager, not only on the NUC
- [ ] You can log into the Backblaze account and see the `lifeos-*` objects
- [ ] HetrixTools monitor exists and points at a contact you actually read
- [ ] You've done at least one **test restore** (even a Postgres-only one) and seen `/api/health` come back OK
- [ ] This runbook is saved somewhere off the NUC
