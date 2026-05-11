# LifeOS

Self-hosted personal life management platform built on document intelligence. Upload documents, auto-extract text (with OCR), get AI-powered classification and summaries, search everything with hybrid semantic + full-text search, and organize by life domain.

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
# Edit .env with your values (at minimum set POSTGRES_PASSWORD, SECRET_KEY, ANTHROPIC_API_KEY)

# 3. Build and start
docker compose up -d --build

# 4. Run Phase 2 migration (if upgrading from Phase 1)
docker exec lifeos-api python migrate_phase2.py

# 5. Verify
curl localhost:8100/api/health
# → {"status":"healthy","database":"ok","qdrant":"ok"}
```

Open **http://localhost:8180** for the web UI.

## Features

### Phase 1 — Foundation
- **Document upload** with drag-and-drop — PDF, images, text files
- **OCR pipeline** — ocrmypdf + Tesseract for scanned documents
- **Hybrid search** — semantic (Qdrant vectors) + full-text (PostgreSQL tsvector), merged with Reciprocal Rank Fusion
- **7 life domains** — medical, financial, auto, home, vet, legal, insurance — each with specific categories
- **Subject tracking** — link documents to people, pets, vehicles, properties
- **Soft deletes** — nothing is permanently removed from the UI
- **Dark-theme SPA** — Alpine.js, no build step, mobile-responsive
- **Agent API auth stub** — API key scoping by domain, ready for bot integrations

### Phase 2 — AI Ingestion Engine
- **AI document analysis** — Claude auto-classifies domain/category, generates summaries, extracts dates
- **Action item extraction** — AI identifies tasks, deadlines, and follow-ups from documents
- **Expiration tracking** — surfaces documents with upcoming expiration dates
- **Q&A interface** — RAG-powered question answering over your document corpus
- **Review queue** — flags low-confidence classifications for human review
- **Re-analysis** — trigger re-analysis of any document with updated prompts
- **Background processing** — AI analysis runs asynchronously, never blocks uploads

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/health` | Health check (DB + Qdrant) |
| `GET` | `/api/stats` | Document counts, storage, domain breakdown, pending actions |
| `GET` | `/api/domains` | List all domains |
| `GET` | `/api/categories` | List categories (optionally by domain) |
| `GET` | `/api/subjects` | List subjects with document counts |
| `POST` | `/api/subjects` | Create subject |
| `GET` | `/api/subjects/{id}` | Subject detail with recent documents |
| `PATCH` | `/api/subjects/{id}` | Update subject |
| `POST` | `/api/documents/upload` | Upload + process + AI analyze document |
| `GET` | `/api/documents` | List documents (filter by domain, category, subject) |
| `GET` | `/api/documents/review` | Review queue (low confidence / failed AI) |
| `GET` | `/api/documents/{id}` | Document detail with AI fields and action items |
| `GET` | `/api/documents/{id}/file` | Download original file |
| `PATCH` | `/api/documents/{id}` | Update metadata / review status |
| `DELETE` | `/api/documents/{id}` | Soft delete |
| `POST` | `/api/documents/{id}/reanalyze` | Re-run AI analysis |
| `GET` | `/api/search?q=...` | Hybrid search |
| `GET` | `/api/search/text?q=...` | Full-text search only |
| `POST` | `/api/ask` | RAG Q&A (question + optional domain filter) |
| `GET` | `/api/expirations` | Documents with upcoming expiration dates |
| `GET` | `/api/actions` | List action items (filter by status, domain, due date) |
| `GET` | `/api/actions/upcoming` | Action items due in next 30 days |
| `GET` | `/api/actions/overdue` | Past-due action items |
| `POST` | `/api/actions` | Create manual action item |
| `PATCH` | `/api/actions/{id}` | Update action item status |

## Project Structure

```
lifeos/
├── api/
│   ├── Dockerfile          # Python 3.12 + OCR dependencies
│   ├── requirements.txt    # Python packages
│   ├── config.py           # Pydantic settings
│   ├── init_db.sql         # Database schema (8 tables)
│   ├── migrate_phase2.py   # Phase 2 migration script
│   ├── main.py             # FastAPI app shell + router includes
│   ├── database.py         # asyncpg pool management
│   ├── constants.py        # Domain/category definitions
│   ├── helpers.py          # Shared utilities (audit_log, etc.)
│   ├── models.py           # Pydantic request/response models
│   ├── ai_analyzer.py      # Claude AI document analysis
│   ├── processor.py        # OCR pipeline + text extraction + chunking
│   ├── search.py           # Embeddings + vector/text search + RRF
│   └── routers/
│       ├── system.py       # Health, stats, domains, categories
│       ├── subjects.py     # Subject CRUD
│       ├── documents.py    # Document upload, CRUD, AI integration
│       ├── search.py       # Search + Q&A + expirations
│       └── actions.py      # Action items CRUD
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

# Run Phase 2 migration
docker exec lifeos-api python migrate_phase2.py

# Database shell
docker exec -it lifeos-postgres psql -U lifeos lifeos

# Qdrant dashboard
# http://localhost:6334/dashboard
```

## Roadmap

- **Phase 3** — Email forwarding ingestion (IMAP polling, attachment extraction)
- **Phase 4** — Mobile PWA capture (camera, multi-page assembly)
- **Phase 5** — Medical module (providers, medications, conditions, health metrics)
- **Phase 6+** — Google Calendar integration, agent bots, dashboards
