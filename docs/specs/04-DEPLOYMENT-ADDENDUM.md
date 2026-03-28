# LifeOS — Deployment & Infrastructure Addendum

> Supplements the phase specs with concrete deployment decisions.
> Place in project root alongside CLAUDE.md.

---

## Deployment Target

**Machine:** Same Ubuntu server currently running Ezekiel
**Runtime:** Docker Compose (separate stack from Ezekiel — no shared services)
**Relationship to Ezekiel:** Fully independent. Separate databases, separate Qdrant instances, separate ports. Ezekiel continues running undisturbed. Future optional migration via bulk import through LifeOS's AI pipeline.

---

## Port Assignments

Ezekiel uses its own port range. LifeOS must not conflict.

| Service | Ezekiel (existing) | LifeOS (new) |
|---------|-------------------|--------------|
| API (FastAPI) | 8000 | 8100 |
| PostgreSQL | 5432 | 5433 |
| Qdrant HTTP | 6333 | 6334 |
| Qdrant gRPC | 6334 | 6335 |
| Nginx (web UI) | 80/443 | 8180 |

LifeOS services bind to these ports on the host. Internally within the Docker network, they use standard ports (5432, 6333, etc.) — the mapping is only at the host level.

---

## Docker Compose — Infrastructure Skeleton

**File:** `docker-compose.yml`

```yaml
version: "3.8"

services:
  lifeos-api:
    build: ./api
    container_name: lifeos-api
    restart: unless-stopped
    ports:
      - "8100:8000"
    environment:
      - DATABASE_URL=postgresql://lifeos:${POSTGRES_PASSWORD}@lifeos-postgres:5432/lifeos
      - QDRANT_URL=http://lifeos-qdrant:6333
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - SECRET_KEY=${SECRET_KEY}
      - DATA_PATH=/data
    volumes:
      - ${DATA_PATH:-/srv/lifeos}/documents:/data/documents
      - ${DATA_PATH:-/srv/lifeos}/auth:/data/auth
    depends_on:
      lifeos-postgres:
        condition: service_healthy
      lifeos-qdrant:
        condition: service_started
    networks:
      - lifeos

  lifeos-postgres:
    image: postgres:16-alpine
    container_name: lifeos-postgres
    restart: unless-stopped
    ports:
      - "5433:5432"
    environment:
      - POSTGRES_USER=lifeos
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
      - POSTGRES_DB=lifeos
    volumes:
      - ${DATA_PATH:-/srv/lifeos}/postgres:/var/lib/postgresql/data
      - ./api/init_db.sql:/docker-entrypoint-initdb.d/init_db.sql
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U lifeos"]
      interval: 5s
      timeout: 5s
      retries: 5
    networks:
      - lifeos

  lifeos-qdrant:
    image: qdrant/qdrant:latest
    container_name: lifeos-qdrant
    restart: unless-stopped
    ports:
      - "6334:6333"
      - "6335:6334"
    volumes:
      - ${DATA_PATH:-/srv/lifeos}/qdrant:/qdrant/storage
    networks:
      - lifeos

  lifeos-nginx:
    image: nginx:alpine
    container_name: lifeos-nginx
    restart: unless-stopped
    ports:
      - "8180:80"
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/nginx.conf:ro
      - ./web/dist:/usr/share/nginx/html:ro
    depends_on:
      - lifeos-api
    networks:
      - lifeos

networks:
  lifeos:
    name: lifeos
```

**Phase 1 starts here.** Cloudflare tunnel service gets added later (not Phase 1).

When Cloudflare Tunnel is added (post-Phase 1), append this service:

```yaml
  lifeos-tunnel:
    image: cloudflare/cloudflared:latest
    container_name: lifeos-tunnel
    restart: unless-stopped
    command: tunnel run
    environment:
      - TUNNEL_TOKEN=${TUNNEL_TOKEN}
    networks:
      - lifeos
```

And configure the tunnel in Cloudflare dashboard to route `lifeos.davidcol.es` (or similar) to `http://lifeos-nginx:80` within the Docker network.

---

## Data Directory Layout

```bash
# Create before first launch
sudo mkdir -p /srv/lifeos/{postgres,qdrant,documents,backups,auth}
sudo mkdir -p /srv/lifeos/documents/{files,scans,import,attachments}
sudo chown -R $USER:$USER /srv/lifeos
```

```
/srv/lifeos/                    # DATA_PATH
├── postgres/                   # PostgreSQL data (mounted volume)
├── qdrant/                     # Qdrant vector storage (mounted volume)
├── documents/
│   ├── files/                  # Stored documents (organized by UUID prefix)
│   │   ├── a3/
│   │   │   └── a3f12c4e-...-doc.pdf
│   │   └── b7/
│   │       └── b7c45e2a-...-scan.jpg
│   ├── scans/                  # Drop folder for batch scanning (future)
│   ├── import/                 # Email import staging (Phase 3)
│   └── attachments/            # Extracted email attachments (Phase 3)
├── auth/                       # OAuth tokens, agent API keys
│   └── google-calendar-tokens.json  (Phase 10)
└── backups/                    # Nightly pg_dump + qdrant snapshots
```

**Separation from Ezekiel:** Ezekiel uses `/srv/ezekiel/`. LifeOS uses `/srv/lifeos/`. No shared directories.

---

## Backup Integration

LifeOS plugs into the existing backup pipeline. Add a new systemd timer/service alongside Ezekiel's:

**File:** `/etc/systemd/system/lifeos-backup.service`

```ini
[Unit]
Description=LifeOS nightly backup
After=docker.service

[Service]
Type=oneshot
ExecStart=/opt/lifeos/scripts/backup.sh
```

**File:** `/etc/systemd/system/lifeos-backup.timer`

```ini
[Unit]
Description=LifeOS nightly backup timer

[Timer]
OnCalendar=*-*-* 02:30:00
Persistent=true

[Install]
WantedBy=timers.target
```

**Backup script** (`scripts/backup.sh`) backs up:
1. PostgreSQL dump: `docker exec lifeos-postgres pg_dump -U lifeos lifeos | gzip > /srv/lifeos/backups/db-$(date +%Y%m%d).sql.gz`
2. Qdrant snapshot: via Qdrant HTTP API
3. Document files: included in restic backup of `/srv/lifeos/`

**Restic** already backs up to the NAS. Add `/srv/lifeos/` to the restic backup paths (or create a separate restic repo for LifeOS). Either way, it flows through the existing NAS → Azure rclone sync.

**Stagger timing:** Ezekiel backs up at 2:00 AM. LifeOS backs up at 2:30 AM. Both finish before the 3:00 AM rclone sync to Azure.

---

## Resource Considerations

Running both stacks on the same server. Approximate resource usage:

| Service | RAM (idle) | RAM (active) | CPU | Disk |
|---------|-----------|-------------|-----|------|
| LifeOS API | ~200MB | ~500MB (during OCR) | Low | Minimal |
| PostgreSQL | ~100MB | ~300MB | Low | Grows with data |
| Qdrant | ~200MB | ~500MB | Low | ~1GB per 100K chunks |
| Nginx | ~10MB | ~10MB | Minimal | Minimal |
| **LifeOS total** | **~510MB** | **~1.3GB** | **Low** | **Grows slowly** |

OCR is the most resource-intensive operation (CPU + RAM spike). The `ocrmypdf` process should be limited:
- `--max-image-mpixels 50` (cap image resolution)
- Consider a processing queue with concurrency=1 to avoid parallel OCR jobs stacking up

**If the server has 16GB+ RAM:** No concerns running both stacks.
**If 8GB:** Workable but monitor Qdrant memory during large reindex operations.
**If less:** Consider moving one stack to the NUC.

---

## Networking — Phase 1 (Local Only)

In Phase 1, LifeOS is accessible only from the local network:
- Web UI: `http://<server-ip>:8180`
- API: `http://<server-ip>:8100`

No authentication is required in Phase 1 since it's LAN-only. When Cloudflare Tunnel is added post-Phase 1, Cloudflare Access provides authentication (email OTP).

**However:** Even for LAN-only, the API should have basic API key validation on agent endpoints from the start. This prevents accidental exposure and means the agent auth layer doesn't need to be retrofitted later.

---

## Project Directory

```bash
# Clone or create project
mkdir -p /opt/lifeos
cd /opt/lifeos

# Recommended structure
/opt/lifeos/
├── api/                        # FastAPI application
├── nginx/                      # Nginx config
├── web/                        # Frontend static files
├── scripts/                    # Backup, maintenance scripts
├── docs/
│   └── specs/                  # Phase spec files (from this project)
├── docker-compose.yml
├── .env                        # Secrets (not in git)
├── .env.example                # Template
├── .gitignore
├── CLAUDE.md                   # Claude Code reference
└── README.md
```

---

## Quick Start Commands

```bash
# First-time setup
cd /opt/lifeos
cp .env.example .env
# Edit .env with: POSTGRES_PASSWORD, ANTHROPIC_API_KEY, SECRET_KEY

# Create data directories
sudo mkdir -p /srv/lifeos/{postgres,qdrant,documents,backups,auth}
sudo mkdir -p /srv/lifeos/documents/{files,scans,import,attachments}
sudo chown -R $USER:$USER /srv/lifeos

# Launch
docker compose up -d --build

# Verify
docker compose ps
curl http://localhost:8100/api/health
curl http://localhost:8100/api/stats

# Seed primary subject
curl -X POST http://localhost:8100/api/subjects \
  -H "Content-Type: application/json" \
  -d '{"name": "Dave", "type": "person", "is_primary": true}'

# View logs
docker compose logs -f lifeos-api

# Database shell
docker exec -it lifeos-postgres psql -U lifeos lifeos

# Rebuild after code changes
docker compose up -d --build lifeos-api

# Stop everything
docker compose down

# Stop everything and remove volumes (DESTRUCTIVE)
docker compose down -v
```

---

## Ezekiel Migration (Optional, Future)

If Dave decides to import Ezekiel's documents into LifeOS:

1. Export Ezekiel documents: `docker exec ezekiel-api python export_documents.py --output /data/export/`
2. Copy export to LifeOS import directory: `cp -r /srv/ezekiel/export/* /srv/lifeos/documents/import/`
3. Run LifeOS bulk import: `docker exec lifeos-api python scripts/bulk_import.py /data/documents/import/`
4. Each document goes through LifeOS's full AI ingestion pipeline — gets re-classified, structured data extracted, action items created
5. Verify import results in LifeOS dashboard
6. Once confirmed, retire Ezekiel: `cd /opt/ezekiel-docs && docker compose down`

This import script doesn't exist yet — it would be written when/if Dave decides to migrate. The key insight is that LifeOS's AI pipeline makes migration valuable, not just a data copy — every old document gets enriched with structured extraction it didn't have before.
