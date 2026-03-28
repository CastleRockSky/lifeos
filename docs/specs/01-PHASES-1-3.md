# LifeOS — Phased Implementation Guide

> For Claude Code execution on Intel NUC (Ubuntu)
> Each phase is a self-contained deliverable with its own testing checklist

---

## Phase Overview

| Phase | Name | Description | Depends On |
|-------|------|-------------|------------|
| 1 | Foundation | Core platform, database, file storage, basic upload, search | None |
| 2 | AI Ingestion | Claude-powered document analysis, auto-categorization, action extraction | Phase 1 |
| 3 | Email Forwarding | Inbound email processing for document ingestion | Phase 2 |
| 4 | Mobile Capture | Camera-first PWA for scanning documents on the go | Phase 2 |
| 5 | Medical Module | Health-specific structured data, HealthBot API integration | Phase 2 |
| 6 | Financial Module | Accounts, debts, tax tracking, FinanceBot API integration | Phase 2 |
| 7 | Auto & Home | Vehicle maintenance, home upkeep, warranty tracking | Phase 2 |
| 8 | Vet & Pets | Pet health tracking, vaccination schedules | Phase 5 (shares health pattern) |
| 9 | Trends & Visualization | Time-series charts, trend analysis, health/financial dashboards | Phases 5-8 |
| 10 | Calendar Integration | Google Calendar sync for action items and reminders | Phase 2 |
| 11 | Insurance & Legal | Cross-cutting insurance tracking, identity document management | Phase 2 |
| 12 | Polish & Hardening | Performance, backup verification, error handling, UX refinement | All |

---

## Phase 1: Foundation

### Goal
Stand up the core platform: database, file storage, document upload, text extraction, basic search, and a functional web UI. This is the Ezekiel skeleton adapted for LifeOS's broader scope.

### Deliverables

#### 1.1 Docker Compose Stack

**File:** `docker-compose.yml`

Services:
- `lifeos-api` — FastAPI application (Python 3.12)
- `lifeos-postgres` — PostgreSQL 16
- `lifeos-qdrant` — Qdrant vector database
- `lifeos-nginx` — Nginx reverse proxy + frontend
- `cloudflared` — Cloudflare Tunnel (optional, for external access)

**Data volumes** mounted from `/srv/lifeos/`:
```
/srv/lifeos/
├── postgres/          # PostgreSQL data
├── qdrant/            # Vector database storage
├── documents/
│   ├── files/         # Stored documents (organized by UUID prefix)
│   ├── scans/         # Drop folder for scanned documents
│   ├── import/        # Email import staging
│   └── attachments/   # Extracted email attachments
└── backups/           # Nightly backups
```

#### 1.2 Database Schema

**File:** `api/init_db.sql`

Create all core tables as defined in the project spec:
- `subjects` — people, pets, vehicles, properties
- `documents` — file metadata, AI analysis results, content text
- `structured_records` — flexible JSONB records by type
- `time_series_metrics` — numeric/text metrics over time
- `action_items` — extracted and manual action items with due dates
- `document_chunks` — text chunks with embedding references
- `audit_log` — all mutations logged

**Indexes:**
- `documents`: domain, category, subject_id, ingested_at
- `structured_records`: domain, record_type, subject_id, next_action_date
- `time_series_metrics`: subject_id + metric_type + recorded_at (composite)
- `action_items`: status + due_date (for "upcoming actions" queries)
- Full-text search index on `documents.content_text`

**Seed data:**
- Create primary subject (Dave, type=person, is_primary=true)
- Create domain enum values
- Create initial category taxonomy (see Appendix A)

#### 1.3 FastAPI Application — Core Routes

**File:** `api/main.py` (modular — split into routers as it grows)

**Document routes:**
```
POST   /api/documents/upload        — Upload file(s), trigger processing
GET    /api/documents                — List documents (filter by domain, category, subject, date range)
GET    /api/documents/{id}           — Get document details + AI analysis
GET    /api/documents/{id}/file      — Download original file
DELETE /api/documents/{id}           — Soft delete (move to trash, retain 30 days)
PATCH  /api/documents/{id}           — Update metadata (title, domain, category, tags)
```

**Search routes:**
```
GET    /api/search?q=...             — Semantic search across all documents
GET    /api/search/text?q=...        — Full-text search (PostgreSQL tsvector)
```

**Subject routes:**
```
GET    /api/subjects                  — List all subjects
POST   /api/subjects                  — Create new subject
GET    /api/subjects/{id}             — Get subject with linked documents and records
PATCH  /api/subjects/{id}             — Update subject profile
```

**System routes:**
```
GET    /api/health                    — Health check (DB + Qdrant status)
GET    /api/stats                     — Document counts, storage usage, index stats
```

#### 1.4 Document Processing Pipeline

**File:** `api/processor.py`

On upload:
1. Detect file type (PDF, image, email)
2. If scanned/image: run OCR via ocrmypdf (deskew, rotate, clean)
3. Extract text content
4. Generate text chunks (512 tokens, 50-token overlap)
5. Generate embeddings via fastembed (BAAI/bge-small-en-v1.5)
6. Store embeddings in Qdrant
7. Store document record in PostgreSQL
8. Return document ID and processing status

#### 1.5 Search Engine

**File:** `api/search.py`

Two search modes:
1. **Semantic search:** Query → embed → Qdrant nearest-neighbor → return ranked document chunks with scores
2. **Full-text search:** PostgreSQL `ts_rank` on `content_text` tsvector column

Combined search merges results with configurable weighting (default: 70% semantic, 30% full-text).

#### 1.6 Frontend — Basic Web UI

**File:** `web/dist/index.html` (SPA, served by Nginx)

Minimal but functional:
- Search bar (prominent, top of page)
- Upload button (drag-and-drop zone)
- Document list with filters (domain, date range)
- Document detail view (metadata, AI summary, original file preview)
- Basic responsive layout (usable on mobile browser, even before PWA phase)

**Tech:** Vanilla HTML/CSS/JS or lightweight framework (Alpine.js or similar). No build step required — keep it deployable as static files.

### Testing Checklist — Phase 1

- [ ] `docker compose up -d` starts all services healthy
- [ ] `/api/health` returns `{"status": "healthy", "database": "ok", "qdrant": "ok"}`
- [ ] Upload a PDF → file stored, text extracted, chunks created, embeddings generated
- [ ] Upload a scanned PDF → OCR runs, text extracted successfully
- [ ] Upload an image (JPG/PNG) → OCR runs, text extracted
- [ ] Search for a term from an uploaded document → returns relevant results
- [ ] Document list loads with correct metadata
- [ ] Document detail view shows file preview and metadata
- [ ] Subject "Dave" exists as primary subject
- [ ] Soft delete works (document hidden from list, file retained)
- [ ] `/api/stats` returns accurate counts
- [ ] Frontend loads and is usable on desktop and mobile browser

---

## Phase 2: AI Ingestion Engine

### Goal
Add Claude-powered document analysis to the ingestion pipeline. Every document gets automatically classified, summarized, and mined for structured data and action items. This is the phase that transforms LifeOS from a file cabinet into an intelligent system.

### Deliverables

#### 2.1 AI Analysis Service

**File:** `api/ai_analyzer.py`

**Core function:** `analyze_document(text: str, file_type: str, user_hint: dict | None) -> AnalysisResult`

Sends extracted document text to Claude (Sonnet) with a comprehensive system prompt. Returns structured JSON with:

```python
@dataclass
class AnalysisResult:
    domain: str                    # medical, financial, auto, home, vet, legal, insurance
    category: str                  # specific category within domain
    subject_hint: str | None       # who/what this is about ("Dave", "Luna the cat", etc.)
    title: str                     # AI-generated descriptive title
    document_date: str | None      # date of the document content (not upload date)
    summary: str                   # 2-3 sentence summary
    extracted_data: dict           # domain-specific structured fields (see below)
    action_items: list[dict]       # [{action, due_date, priority, recurrence}]
    metrics_to_log: list[dict]     # [{type, value, unit, date}]
    related_records: list[dict]    # [{record_type, match_hint}] for linking
    expiration_date: str | None    # if this document expires (license, policy, etc.)
    tags: list[str]                # searchable tags
    confidence: float              # 0-1, how confident the AI is in classification
```

#### 2.2 AI System Prompt — Document Classifier

The classification prompt must handle all domains. Structure:

```
You are a document analysis system for a personal life management platform.
Analyze the following document text and extract structured information.

DOMAINS AND CATEGORIES:
[exhaustive list — see Appendix A]

EXTRACTION RULES:
- Identify the domain and most specific category
- Extract dates in ISO format
- For medical: extract provider, diagnosis codes, medications, lab values
- For financial: extract account numbers (last 4 only), amounts, rates, dates
- For auto: extract VIN, mileage, service type, parts, costs
- For vet: extract pet name, provider, vaccinations, medications
- For insurance: extract policy number, coverage type, premium, deductible, dates
- For legal: extract document type, parties, dates, expiration

ACTION ITEM RULES:
- Only extract actions that require Dave to DO something
- Include due dates when stated or inferable
- Flag urgency: "follow up with doctor" from a lab result = medium priority
- "Prescription expires 2025-04-01" = high priority if within 30 days
- Recurring items: specify recurrence (annual, monthly, etc.)

SUBJECT MATCHING:
- Default subject is "Dave" for medical, financial, legal documents
- Look for pet names, vehicle descriptions, property addresses
- If uncertain, set subject_hint to null — user will confirm

Respond ONLY with valid JSON matching the schema below. No preamble.
[schema]
```

#### 2.3 Updated Ingestion Pipeline

Modify `processor.py` to integrate AI analysis:

```
Upload → OCR (if needed) → Text Extract → AI Analysis → Store
                                              │
                                              ├─ Create/update structured_records
                                              ├─ Log time_series_metrics
                                              ├─ Create action_items
                                              ├─ Link to existing subjects
                                              └─ Flag low-confidence items for review
```

**Low-confidence handling:** If `confidence < 0.7`, the document is stored but flagged for manual review. The UI shows a "needs review" badge. Dave can correct the classification and the system learns from corrections (stored as training examples for prompt refinement).

#### 2.4 Action Items Management

**New routes:**
```
GET    /api/actions                   — List actions (filter: status, domain, due_date range)
GET    /api/actions/upcoming?days=7   — Actions due in next N days
GET    /api/actions/overdue           — Overdue actions
PATCH  /api/actions/{id}              — Update status (complete, snooze, dismiss)
POST   /api/actions                   — Create manual action item
```

**Dashboard component:** "Action Center" widget showing:
- Overdue items (red)
- Due this week (amber)
- Upcoming 30 days (neutral)
- Recently completed (dimmed)

#### 2.5 Expiration Tracking

**New route:**
```
GET    /api/expirations?days=90       — Documents/records expiring in next N days
```

Scans both `documents.expiration_date` and `structured_records` where `next_action_date` is within range. Powers the "expiring soon" dashboard widget.

#### 2.6 AI Q&A Interface

**New route:**
```
POST   /api/ask                       — Natural language question → AI answer with citations
```

Flow:
1. User asks a question ("when is my dog's next vaccination due?")
2. Semantic search finds relevant document chunks
3. Also query structured_records for matching data
4. Send question + context to Claude
5. Return answer with source citations (document IDs, record IDs)

#### 2.7 Frontend Updates

- AI analysis results displayed on document detail page
- Action Center dashboard widget
- Expiration alerts banner
- AI Q&A chat interface (simple — input box, response area with citations)
- "Needs review" queue for low-confidence classifications
- Manual classification override UI

### Testing Checklist — Phase 2

- [ ] Upload a medical document (lab result PDF) → correctly classified as medical/lab_result
- [ ] Upload a financial document (credit card statement) → correctly classified as financial
- [ ] Upload a vet record → correctly identified pet name as subject
- [ ] AI extracts action items with due dates from a doctor's note
- [ ] AI extracts metrics (lab values) and logs to time_series_metrics
- [ ] AI identifies document expiration date (insurance policy, vehicle registration)
- [ ] Low-confidence document gets flagged for review
- [ ] Manual classification override works and saves
- [ ] Action Center shows upcoming and overdue items correctly
- [ ] Expiration tracker surfaces items expiring within 90 days
- [ ] AI Q&A returns accurate answers with source citations
- [ ] AI Q&A correctly says "I don't have information about that" when appropriate
- [ ] Uploaded documents without text (blank image) handled gracefully

---

## Phase 3: Email Forwarding Ingestion

### Goal
Set up a dedicated email address that accepts forwarded documents. Dave forwards an email from his vet, insurance company, or doctor's office → LifeOS extracts attachments, processes them through the AI pipeline, and files everything automatically.

### Deliverables

#### 3.1 Email Receiving

**Approach:** IMAP polling (simplest, most reliable)

**Setup:**
- Create a dedicated Gmail address or use an alias on Dave's existing Google Workspace (`docs@davidcol.es` or similar)
- Configure IMAP access
- LifeOS polls every 2 minutes for new messages

**Alternative (future):** Webhook via Cloudflare Email Workers for instant processing. But IMAP polling is simpler to start.

#### 3.2 Email Processing Service

**File:** `api/email_ingest.py`

On new email received:
1. Parse email (from, subject, body, attachments)
2. For each attachment:
   a. Save to staging directory
   b. Run through the full ingestion pipeline (OCR → AI analysis → store)
   c. Include email metadata as context for AI analysis (subject line, sender often gives domain hints)
3. Store email body as metadata on the document record
4. Handle common patterns:
   - Forwarded emails (strip "Fwd:" prefix, extract original sender)
   - Inline images vs. attachments
   - PDF attachments with names like "EOB_03152025.pdf" → domain hints
   - Emails with no attachments but important text content (appointment confirmations) → store as text document

#### 3.3 Sender Mapping (Smart Routing)

**Table:** `email_sender_map`
```
sender_pattern     | domain    | category    | subject_hint
*@questdiagnostics | medical   | lab_result  | Dave
*@vetclinic.com    | vet       | visit_note  | null (AI determines pet)
*@statefarm.com    | insurance | null        | null (AI determines type)
```

As Dave forwards emails, the system builds a sender → domain mapping. After a few documents from the same sender, it can pre-classify before AI analysis — faster processing, higher confidence.

#### 3.4 Email Processing Status

**New routes:**
```
GET    /api/email/status              — Recent email processing log
GET    /api/email/queue               — Pending/failed email processing
POST   /api/email/retry/{id}          — Retry failed email processing
```

**Dashboard widget:** Shows recent email ingestions with status (processed, failed, needs review).

### Testing Checklist — Phase 3

- [ ] IMAP polling connects and reads new emails
- [ ] Forward an email with a PDF attachment → attachment processed through full pipeline
- [ ] Forward an email with an image attachment → OCR runs, document processed
- [ ] Forward an email with multiple attachments → each processed separately
- [ ] Forwarded email metadata (original sender, subject) stored and used by AI
- [ ] Email with no attachment but appointment confirmation text → stored as text document
- [ ] Sender mapping builds over time (second email from same sender auto-classifies)
- [ ] Failed email processing surfaces in dashboard with retry option
- [ ] Duplicate email detection (forwarding same email twice doesn't create duplicates)
- [ ] Email from unknown sender processes correctly (AI classifies without sender hints)
