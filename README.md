# LifeOS

Self-hosted personal life management platform built on document intelligence. Upload documents, auto-extract text (with OCR), search everything with hybrid semantic + full-text search, and organize by life domain.

## Architecture

| Service | Port | Purpose |
|---------|------|---------|
| **PostgreSQL 16** | 5433 | Relational data, full-text search |
| **Qdrant** | 6334 | Vector embeddings for semantic search |
| **FastAPI** (Python 3.12) | 8100 | API server |
| **Nginx** | 8180 | Reverse proxy + static frontend |

All services run in Docker. Persistent data lives under `/srv/lifeos/`.

## Quick Start

```bash
# 1. Create data directories
sudo mkdir -p /srv/lifeos/documents/files /srv/lifeos/postgres /srv/lifeos/qdrant
sudo chown -R $USER:$USER /srv/lifeos

# 2. Configure environment
cp .env.example .env
# Edit .env with your values (at minimum set POSTGRES_PASSWORD and SECRET_KEY)

# 3. Build and start
docker compose up -d --build

# 4. Verify
curl localhost:8100/api/health
# → {"status":"healthy","database":"ok","qdrant":"ok"}
```

Open **http://localhost:8180** for the web UI.

## Features (Phase 1)

- **Document upload** with drag-and-drop — PDF, images, text files
- **OCR pipeline** — ocrmypdf + Tesseract for scanned documents
- **Hybrid search** — semantic (Qdrant vectors) + full-text (PostgreSQL tsvector), merged with Reciprocal Rank Fusion
- **7 life domains** — medical, financial, auto, home, vet, legal, insurance — each with specific categories
- **Subject tracking** — link documents to people, pets, vehicles, properties
- **Soft deletes** — nothing is permanently removed from the UI
- **Dark-theme SPA** — Alpine.js, no build step, mobile-responsive
- **Agent API auth stub** — API key scoping by domain, ready for bot integrations

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/health` | Health check (DB + Qdrant) |
| `GET` | `/api/stats` | Document counts, storage, domain breakdown |
| `GET` | `/api/domains` | List all domains |
| `GET` | `/api/categories` | List categories (optionally by domain) |
| `GET` | `/api/subjects` | List subjects with document counts |
| `POST` | `/api/subjects` | Create subject |
| `GET` | `/api/subjects/{id}` | Subject detail with recent documents |
| `PATCH` | `/api/subjects/{id}` | Update subject |
| `POST` | `/api/documents/upload` | Upload + process document |
| `GET` | `/api/documents` | List documents (filter by domain, category, subject) |
| `GET` | `/api/documents/{id}` | Document detail with chunks |
| `GET` | `/api/documents/{id}/file` | Download original file |
| `PATCH` | `/api/documents/{id}` | Update metadata |
| `DELETE` | `/api/documents/{id}` | Soft delete |
| `GET` | `/api/search?q=...` | Hybrid search |
| `GET` | `/api/search/text?q=...` | Full-text search only |

## Project Structure

```
lifeos/
├── api/
│   ├── Dockerfile          # Python 3.12 + OCR dependencies
│   ├── requirements.txt    # Python packages
│   ├── config.py           # Pydantic settings
│   ├── init_db.sql         # Database schema (8 tables)
│   ├── main.py             # FastAPI app + all routes
│   ├── processor.py        # OCR pipeline + text extraction + chunking
│   └── search.py           # Embeddings + vector/text search + RRF
├── nginx/
│   └── nginx.conf          # Reverse proxy config
├── web/
│   └── dist/
│       └── index.html      # Alpine.js SPA (single file, no build step)
├── docs/
│   └── specs/              # Project specifications
├── docker-compose.yml
├── .env.example
└── CLAUDE.md               # Development reference
```

## Common Commands

```bash
# Rebuild and restart API
docker compose up -d --build api

# View logs
docker compose logs -f api

# Database shell
docker exec -it lifeos-postgres psql -U lifeos lifeos

# Qdrant dashboard
# http://localhost:6334/dashboard
```

## Roadmap

- **Phase 2** — Claude AI document analysis, auto-classification, action item extraction
- **Phase 3** — Structured records (medications, accounts, policies), time-series metrics
- **Phase 4** — Google Calendar integration, proactive reminders
- **Phase 5** — Agent API (HealthBot, FinanceBot, etc.)
- **Phase 6+** — Mobile PWA, Cloudflare Tunnel, email ingestion, dashboards
