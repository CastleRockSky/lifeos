# LifeOS — Personal Life Management & Document Intelligence Platform

> Project Specification v1.0
> Built on the Ezekiel document intelligence foundation
> Designed for Claude Code phased implementation

---

## Vision

LifeOS is a self-hosted life management platform that combines document storage with structured data tracking, trend analysis, and actionable integrations. It's built for one person (Dave) to manage the full complexity of adult life — medical records, finances, vehicle maintenance, pet care, home upkeep — in a single searchable, AI-powered system.

**The core promise:** Drop in a document (scan, upload, or email forward), and LifeOS reads it, extracts structured data, files it in the right place, flags actions needed, and makes it searchable forever. Ask "when is my dog's next vaccination due?" or "what's my total monthly debt obligation?" and get an instant, cited answer.

**Design philosophy:**
- **AI-first ingestion.** Claude reads every document on intake, extracts metadata, categorizes, and flags action items. Manual tagging is a fallback, not the default workflow.
- **Structured + unstructured.** Documents (PDFs, scans, images) live alongside structured records (metrics, accounts, schedules). They reference each other.
- **ADHD-optimized.** Minimum friction for input. Proactive surfacing of important items. No "homework" — the system does the organizing.
- **Privacy by default.** Self-hosted, local embeddings, health data encrypted at rest. Nothing leaves your network except explicit API calls to Claude for Q&A.
- **Agent-compatible.** Exposes a REST API that OpenClaw agents (HealthBot, FinanceBot, etc.) can read from and write to. LifeOS is the data backbone; agents are the conversational interface.

---

## Architecture Overview

```
Ingestion Layer                    Core Platform                     Output Layer
┌─────────────────┐               ┌──────────────────┐              ┌──────────────────┐
│ Web Upload (UI) │──┐            │    FastAPI        │              │ Web Dashboard    │
│ Email Forward   │──┤   OCR +    │    ─────────      │──────────────│ AI Search / Q&A  │
│ Mobile Capture  │──┤── AI ──────│  PostgreSQL 16    │              │ Trend Charts     │
│ API (Agents)    │──┘  Ingest    │  Qdrant (vectors) │              │ Action Items     │
└─────────────────┘               │  File Storage     │              │ Expiration Alerts│
                                  └────────┬─────────┘              │ REST API (agents)│
                                           │                        └──────────────────┘
                                  ┌────────▼─────────┐
                                  │  Integrations     │
                                  │  Google Calendar   │
                                  │  OpenClaw Agents   │
                                  └──────────────────┘
```

**Stack (inherited from Ezekiel, extended):**
- PostgreSQL 16 — metadata, structured records, categories, audit log
- Qdrant — vector embeddings for semantic search (local, fastembed)
- FastAPI — API server, ingestion pipeline, AI query engine
- Nginx — reverse proxy, static frontend
- ocrmypdf / Tesseract — OCR pipeline for scans and photos
- Claude API — document understanding, Q&A, action extraction
- fastembed (BAAI/bge-small-en-v1.5) — local embeddings (no data leaves network)
- Cloudflare Tunnel + Access — secure external access with email OTP

---

## Domain Modules

LifeOS is organized into domain modules. Each module has its own document categories, structured data tables, and dashboard views, but they all share the same search index, AI interface, and storage infrastructure.

### 1. Medical & Health
- **Documents:** Doctor's notes, lab results, imaging reports, prescriptions, insurance EOBs, vaccination records
- **Structured data:** Medications (name, dose, frequency, prescriber, start date), providers (name, specialty, phone, portal URL, next appointment), health metrics (weight, BP, labs — time-series), conditions/diagnoses
- **Subjects:** Supports multiple subjects (Dave, family members) — each with their own profile
- **Actions:** Prescription refill reminders, appointment follow-ups, lab orders, specialist referrals
- **Agent integration:** HealthBot reads/writes health metrics and medication lists via API

### 2. Financial
- **Documents:** Tax returns, W-2s/1099s, loan agreements, insurance policies, investment statements, receipts
- **Structured data:** Accounts (bank, credit, investment — balance, rate, minimum payment), debts (creditor, balance, rate, minimum, payoff date), recurring expenses, tax timeline items
- **Actions:** Bill due dates, tax deadlines, policy renewals, debt payoff tracking
- **Agent integration:** FinanceBot reads account balances and debt summary via API

### 3. Auto & Vehicles
- **Documents:** Registration, title, insurance cards, service receipts, recall notices, purchase/lease agreements
- **Structured data:** Vehicles (year, make, model, VIN, mileage log), maintenance schedule (oil change, tires, inspection — with interval tracking), insurance policies
- **Actions:** Registration renewal, oil change due, inspection due, insurance renewal

### 4. Home & Property
- **Documents:** Mortgage/lease, HOA docs, insurance policy, appliance warranties, contractor invoices, permits
- **Structured data:** Property info, mortgage details, appliances (make, model, install date, warranty expiration), contractors (name, trade, phone, rating, last used)
- **Actions:** Warranty expirations, filter replacements, seasonal maintenance reminders, insurance renewal

### 5. Veterinary & Pets
- **Documents:** Vaccination records, vet visit notes, prescription receipts, insurance policies, adoption/registration papers
- **Structured data:** Pets (name, species, breed, DOB, weight log, microchip #), medications, providers, vaccination schedule
- **Actions:** Vaccination due dates, prescription refills, annual checkup reminders, flea/tick/heartworm schedule
- **Agent integration:** Same health tracking pattern as medical module but for pet subjects

### 6. Legal & Identity
- **Documents:** IDs (passport, driver's license — expiration tracked), birth certificates, marriage certificate, will/trust documents, power of attorney, SSN cards
- **Structured data:** Document expiration dates, account recovery info (non-sensitive)
- **Actions:** Passport/license renewal reminders, will review reminders

### 7. Insurance (Cross-Cutting)
- **Documents:** Policy declarations, premium notices, claims, EOBs
- **Structured data:** Policies (type, carrier, policy #, premium, deductible, coverage limits, renewal date) — linked to relevant domain (health, auto, home, pet)
- **Actions:** Premium due dates, renewal windows, coverage review reminders

---

## Data Model (Simplified)

### Core Tables

```
documents
├── id (uuid)
├── title
├── file_path
├── file_type (pdf, image, email, scan)
├── domain (medical, financial, auto, home, vet, legal, insurance)
├── category (more specific: lab_result, tax_return, service_receipt, etc.)
├── subject_id → subjects (nullable — who/what this document is about)
├── source (upload, email_forward, mobile_capture, api)
├── ingested_at
├── ai_summary (Claude-generated on intake)
├── ai_extracted_data (JSON — structured fields Claude pulled from the doc)
├── ai_action_items (JSON — flagged actions with dates)
├── content_text (extracted/OCR text for search)
├── embedding_status
└── tags[]

subjects
├── id (uuid)
├── name
├── type (person, pet, vehicle, property)
├── profile_data (JSON — flexible per type)
└── is_primary (boolean — is this Dave?)

structured_records
├── id (uuid)
├── domain
├── record_type (medication, account, vehicle, policy, provider, etc.)
├── subject_id → subjects
├── data (JSONB — flexible schema per record_type)
├── source_document_id → documents (nullable — links to supporting doc)
├── valid_from / valid_to (temporal — when is this record "current"?)
├── next_action_date (nullable — when does something need to happen?)
├── next_action_description
└── created_at / updated_at

time_series_metrics
├── id
├── subject_id → subjects
├── metric_type (weight, blood_pressure, mileage, account_balance, etc.)
├── value_numeric
├── value_text (for non-numeric like "120/80")
├── recorded_at
├── source (manual, document_extract, agent_api)
└── notes

action_items
├── id (uuid)
├── domain
├── subject_id → subjects
├── title
├── description
├── due_date
├── source_type (ai_extracted, manual, recurring)
├── source_document_id → documents (nullable)
├── source_record_id → structured_records (nullable)
├── status (pending, completed, snoozed, dismissed)
├── calendar_event_id (nullable — if synced to Google Calendar)
├── recurrence_rule (nullable — RRULE format for recurring items)
└── created_at / completed_at

document_chunks (existing from Ezekiel — for semantic search)
├── id
├── document_id → documents
├── chunk_text
├── chunk_index
├── embedding (vector)
└── metadata (JSON)
```

### Key Design Decisions

1. **JSONB for structured records.** Rather than creating 15 different tables for medications, vehicles, policies, etc., we use a single `structured_records` table with a `record_type` discriminator and JSONB `data` column. This keeps the schema manageable while allowing each record type to have its own shape. JSON schemas for each record type are defined in code and validated on write.

2. **Subjects are first-class.** Everything relates to a subject — Dave, his pets, his vehicles, his property. This makes queries like "show me everything about [pet name]" trivial.

3. **Time-series is separate.** Metrics that accumulate over time (weight, BP, mileage, balances) live in a dedicated table optimized for range queries and trend computation. This is what powers charts and trend analysis.

4. **Action items are extracted, not just tagged.** When Claude reads a document on intake, it identifies action items (follow-up appointment, prescription refill, registration renewal) and creates them as first-class records with due dates — not just notes buried in a summary.

5. **Documents link to structured records.** A vet visit PDF links to the pet subject, the provider record, and any vaccination records it updates. This bidirectional linking is what makes "show me everything about Fluffy's vaccinations" work.

---

## AI Ingestion Pipeline

This is the highest-value feature and the most important to get right. When a document enters the system:

```
Document arrives (upload / email / photo / API)
    │
    ▼
┌─ OCR if needed (scanned PDF, photo) ─────────────────┐
│  ocrmypdf with deskew, rotate, clean                  │
└───────────────────────────────────────────────────────┘
    │
    ▼
┌─ Text Extraction ────────────────────────────────────┐
│  Extract raw text from PDF/image/email               │
└──────────────────────────────────────────────────────┘
    │
    ▼
┌─ Claude AI Analysis ────────────────────────────────┐
│  Send text + system prompt to Claude                 │
│  Returns structured JSON:                            │
│  {                                                   │
│    "domain": "medical",                              │
│    "category": "lab_result",                         │
│    "subject": "Dave",                                │
│    "title": "Quest Diagnostics - Lipid Panel",       │
│    "summary": "...",                                 │
│    "date": "2025-03-15",                             │
│    "extracted_data": {                               │
│      "provider": "Quest Diagnostics",                │
│      "ordering_physician": "Dr. Smith",              │
│      "results": [                                    │
│        {"test": "Total Cholesterol", "value": 210,   │
│         "unit": "mg/dL", "range": "< 200",           │
│         "flag": "high"}                              │
│      ]                                               │
│    },                                                │
│    "action_items": [                                  │
│      {"action": "Follow up with Dr. Smith on high    │
│        cholesterol", "due": "2025-04-15",            │
│        "priority": "medium"}                         │
│    ],                                                │
│    "metrics_to_log": [                               │
│      {"type": "cholesterol_total", "value": 210,     │
│       "date": "2025-03-15"}                          │
│    ],                                                │
│    "related_records": [                               │
│      {"type": "provider", "match_hint": "Dr. Smith"} │
│    ],                                                │
│    "expiration_date": null,                          │
│    "tags": ["bloodwork", "lipids", "cholesterol"]    │
│  }                                                   │
└──────────────────────────────────────────────────────┘
    │
    ▼
┌─ Storage & Indexing ─────────────────────────────────┐
│  1. Store file in /srv/lifeos/documents/files/       │
│  2. Create document record in PostgreSQL             │
│  3. Create/update structured_records as needed       │
│  4. Log time-series metrics if extracted             │
│  5. Create action_items with due dates               │
│  6. Generate embeddings, store in Qdrant             │
│  7. Link to existing subjects/records                │
│  8. Queue calendar events if actionable              │
└──────────────────────────────────────────────────────┘
```

### AI Classification Prompt (Core)

The system prompt for document classification is critical. It needs to be:
- Exhaustive in the extraction schema (so Claude knows what to look for)
- Domain-aware (medical docs need different extraction than financial)
- Conservative with subjects (match existing subjects, don't create new ones without confirmation)
- Explicit about action items (dates, priorities, recurrence)

This prompt is defined in Phase 2 implementation.

---

## Ingestion Methods

### 1. Web Upload (Phase 1)
Standard file upload through the web UI. Drag-and-drop, multi-file support. User can optionally provide domain hint ("this is a medical document") but AI classification handles it automatically.

### 2. Email Forwarding (Phase 3)
Dedicated email address (e.g., `docs@lifeos.davidcol.es`) that accepts forwarded emails. The system:
- Receives via IMAP polling or webhook
- Extracts attachments (PDFs, images)
- Processes each attachment through the ingestion pipeline
- Stores the email body as context metadata
- Handles common patterns: forwarded insurance EOBs, emailed receipts, vet records sent by the clinic

### 3. Mobile Capture (Phase 4)
Progressive Web App (PWA) or lightweight mobile page that:
- Opens camera directly
- Captures one or more photos
- Uploads to LifeOS API
- Triggers OCR + AI ingestion pipeline
- Designed for: scanning paper receipts, medication labels, vet paperwork at the clinic, registration stickers

---

## Agent API (OpenClaw Integration)

LifeOS exposes a REST API for OpenClaw agents to interact with structured data:

```
GET  /api/agent/health/metrics?subject=dave&type=weight&days=30
GET  /api/agent/health/medications?subject=dave&active=true
POST /api/agent/health/metrics   (HealthBot logging a weigh-in)
GET  /api/agent/finance/debts?summary=true
GET  /api/agent/finance/accounts?type=credit
GET  /api/agent/pets/{name}/vaccinations
GET  /api/agent/vehicles/{id}/maintenance-due
GET  /api/agent/actions/upcoming?days=7
POST /api/agent/actions/{id}/complete
```

Authentication: API key per agent, scoped to their domain. HealthBot can read/write health data but not financial data. FinanceBot can read financial data but not health data.

---

## Google Calendar Integration

Action items with due dates can be synced to Google Calendar:
- **One-way sync (LifeOS → Calendar):** LifeOS creates calendar events for actionable items. Events include a link back to the LifeOS record.
- **Scope:** Uses the same Google Cloud project (`iconic-monitor-489123-s4`) and OAuth flow as the OpenClaw agents.
- **Calendar:** A dedicated "LifeOS" calendar within Dave's Google account — keeps life management items separate from personal/work events.
- **Recurrence:** Handles recurring items (monthly mortgage payment, quarterly oil change, annual pet vaccination) via RRULE.

---

## Dashboard & UI

### Primary Views

1. **Search (Home)** — AI-powered search bar front and center. Natural language queries. Results with source citations and links to documents.

2. **Action Center** — Upcoming actions, overdue items, expiring documents. Grouped by domain. One-click complete/snooze/dismiss. This is the "what do I need to deal with?" view.

3. **Domain Views** — Per-domain dashboards:
   - Medical: providers, medications, upcoming appointments, recent lab trends
   - Financial: account balances, debt overview, upcoming payments, tax timeline
   - Auto: vehicles, maintenance schedule, upcoming service
   - Home: maintenance calendar, warranty tracker, contractor list
   - Vet: per-pet health cards, vaccination schedule, upcoming appointments
   - Legal: document inventory, expiration tracker

4. **Trends** — Time-series charts for tracked metrics. Weight over time, BP trends, debt paydown progress, mileage between services.

5. **Recent Activity** — Chronological feed of recently ingested documents and logged data points.

---

## Security & Privacy

- **Self-hosted** on Dave's infrastructure (Docker on the NUC or dedicated server)
- **Cloudflare Access** for authentication (email OTP, same as Ezekiel)
- **Local embeddings** via fastembed — document text never leaves the network for search
- **Claude API** calls are the only external data transmission — used for Q&A and document analysis. Sent over HTTPS, subject to Anthropic's data retention policies.
- **Health data encryption** at rest for the medical domain (PostgreSQL column-level encryption for sensitive fields)
- **Agent API keys** scoped by domain — least-privilege access
- **Backup architecture** inherited from Ezekiel: restic → NAS → Azure Blob (encrypted, deduplicated)

---

## What This Is NOT

- **Not a replacement for medical records portals.** LifeOS stores YOUR copies of medical documents and tracks YOUR metrics. It doesn't connect to Epic/MyChart/etc.
- **Not an accounting system.** It tracks account balances and debts for visibility but doesn't do double-entry bookkeeping. QuickBooks handles that. YNAB handles budgeting.
- **Not a calendar app.** It pushes action items TO Google Calendar. It doesn't replace it.
- **Not a doctor, lawyer, or financial advisor.** AI answers are informational. The system surfaces data and trends — decisions are Dave's.
