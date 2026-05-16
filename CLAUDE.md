# CLAUDE.md — LifeOS Development Reference

> Primary reference for Claude Code when working on this project.
> Read the phase-specific spec before starting any phase.

---

## Project Overview

LifeOS is a self-hosted personal life management platform built on the Ezekiel document intelligence system. It combines AI-powered document storage with structured data tracking, trend analysis, and agent API integration.

**Owner:** Dave (Castle Rock, CO)
**Deployment target:** Intel NUC running Ubuntu Desktop (Docker Compose)
**Spec files location:** `/docs/specs/` (copy from project root)

---

## Architecture

- **FastAPI** (Python 3.12) — API server
- **PostgreSQL 16** — relational data
- **Qdrant** — vector embeddings for semantic search
- **Nginx** — reverse proxy + static frontend
- **ocrmypdf + Tesseract** — OCR pipeline
- **fastembed** (BAAI/bge-small-en-v1.5) — local embeddings
- **Claude API** (Sonnet) — document analysis + Q&A
- **Cloudflare Tunnel** — external access

All services run in Docker. Persistent data lives under `/srv/lifeos/`.

---

## Key Design Principles

1. **AI-first ingestion.** Every document goes through Claude for classification, extraction, and action flagging. Manual tagging is a fallback.

2. **Structured + unstructured hybrid.** Documents (files) and structured records (JSONB) coexist. They reference each other via foreign keys.

3. **ADHD-optimized UX.** Minimum friction for input. Proactive surfacing. No "homework." If something requires more than 2 clicks, redesign it.

4. **Agent-compatible API.** OpenClaw agents (HealthBot, FinanceBot, etc.) interact via authenticated REST endpoints scoped by domain.

5. **Privacy by default.** Local embeddings, encrypted health data, self-hosted everything. Only Claude API calls leave the network.

---

## Database Schema Principles

- **`subjects` table** is the universal entity — people, pets, vehicles, properties. Everything links to a subject.
- **`structured_records`** uses JSONB with a `record_type` discriminator. Each record type has a JSON schema defined in `api/schemas/`. Validate on write.
- **`time_series_metrics`** is optimized for range queries. Composite index on (subject_id, metric_type, recorded_at).
- **`action_items`** are first-class. AI extracts them from documents. They have due dates, statuses, and optional calendar event links.
- **Soft deletes everywhere.** Documents and records are never hard-deleted from the UI — they get a `deleted_at` timestamp.

---

## API Design Patterns

### Authentication
- **Web UI:** Cloudflare Access (email OTP via Cloudflare Tunnel)
- **Agent API:** API key in `X-Agent-Key` header, scoped by domain
- **Internal:** No auth needed for health checks

### Response Format
```json
{
  "data": { ... },
  "meta": {
    "total": 42,
    "page": 1,
    "per_page": 20
  }
}
```

### Error Format
```json
{
  "error": "not_found",
  "message": "Document with ID xyz not found",
  "details": {}
}
```

### Agent API Scoping
Each agent key has a list of allowed domains. Enforce at the router level:
```python
def require_domain(domain: str):
    def dependency(agent_key: str = Header(alias="X-Agent-Key")):
        allowed = validate_agent_key(agent_key)
        if domain not in allowed:
            raise HTTPException(403, "Agent not authorized for this domain")
    return Depends(dependency)
```

---

## AI Integration Notes

### Document Analysis
- Model: Claude Sonnet (latest stable)
- System prompt defined in `api/ai_analyzer.py`
- Response format: strict JSON (no markdown, no preamble)
- Timeout: 60 seconds (some documents are long)
- Cost control: truncate input to ~8000 tokens for analysis (most documents don't need more)
- Fallback: if API fails, store document without analysis, queue for retry

### Q&A
- Model: Claude Sonnet
- Context: semantic search results (top 10 chunks) + relevant structured records
- Response includes source citations (document IDs)
- Cost control: limit context to ~4000 tokens of chunks

### Prompt Versioning
Store the current analysis prompt version in the database. When the prompt changes, optionally re-analyze existing documents with the new prompt.

---

## File Storage

Documents stored at: `/srv/lifeos/documents/files/{uuid_prefix}/{uuid}.{ext}`

UUID prefix = first 2 characters of UUID, for filesystem distribution:
```
/srv/lifeos/documents/files/
├── a3/
│   ├── a3f12c4e-...pdf
│   └── a3b89d1f-...jpg
├── b7/
│   └── b7c45e2a-...pdf
```

---

## Testing

Automated tests live in `api/tests/` (pytest). See Common Commands for how to run
them. Unit tests cover pure logic (recurrence dates, MIME classification, agent-key
hashing, rate limiting, domain constants); `integration`-marked tests hit a live API
and self-skip when the stack is down. Add tests alongside new pure functions and
endpoints.

Each phase also has a manual testing checklist in the spec. Before considering a
phase complete:
1. All checklist items pass
2. No regressions in prior phase functionality
3. API error responses are clean (no stack traces)
4. Mobile browser tested for new UI components

---

## Common Commands

```bash
# Rebuild and restart
docker compose up -d --build api

# View logs
docker compose logs -f api

# Database shell
docker exec -it lifeos-postgres psql -U lifeos lifeos

# Qdrant dashboard
# http://localhost:6333/dashboard

# Run a specific script
docker exec lifeos-api python scripts/reindex.py

# Manual backup
sudo systemctl start lifeos-backup.service

# Run the test suite (pytest is dev-only — not in the prod image)
docker exec lifeos-api pip install -q -r requirements-dev.txt
docker exec -w /app lifeos-api python -m pytest                      # all tests
docker exec -w /app lifeos-api python -m pytest -m "not integration" # unit only, no running stack needed
```

---

## Known Gotchas

1. **fastembed first load:** Downloads the model (~130MB) on first embedding generation. Pre-pull in the Dockerfile.
2. **OCR memory:** ocrmypdf on large scanned PDFs can spike memory. Set `--max-image-mpixels 50` to cap.
3. **Qdrant collection:** Must be created before first embedding insert. Handle in app startup.
4. **Google Calendar timezone:** All events must include timezone (America/Denver). Omitting it causes all-day events to shift.
5. **JSONB indexing:** For frequent queries on structured_records.data fields, add GIN indexes on commonly queried paths.
6. **Claude API cost:** Document analysis at ~$0.01-0.03 per document (Sonnet). Q&A at ~$0.01-0.05 per query. Budget for 500+ documents/month during initial loading.
