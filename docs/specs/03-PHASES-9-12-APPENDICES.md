# LifeOS — Phases 9–12: Visualization, Calendar, Insurance/Legal & Polish

---

## Phase 9: Trends & Visualization

### Goal
Transform accumulated time-series data into meaningful visual trends. Weight over months, BP trends, debt paydown progress, maintenance cost history — the patterns that emerge from consistent tracking.

### Deliverables

#### 9.1 Chart Engine

**Backend:** Pre-compute trend data as API endpoints. Frontend renders with Chart.js or similar.

**Trend API routes:**
```
GET /api/trends/{subject_id}/{metric_type}
    ?period=daily|weekly|monthly
    &range=30d|90d|6m|1y|all
    &include_goal=true

Returns:
{
  "data_points": [{"date": "2025-03-01", "value": 186.2}, ...],
  "average": 184.8,
  "min": 182.1,
  "max": 188.4,
  "trend_direction": "down",
  "trend_rate": -0.8,  // units per week
  "goal": 175,
  "projected_goal_date": "2025-08-15"
}
```

#### 9.2 Chart Types

| Chart | Data Source | Use |
|-------|-----------|-----|
| Weight trend line | time_series_metrics (weight) | Weekly averages with goal line |
| BP chart | time_series_metrics (systolic + diastolic) | Dual line with normal range bands |
| Debt paydown | structured_records (loans + credit) | Stacked area, total decreasing over time |
| Workout consistency | action_items (workout type, completed) | Calendar heatmap (GitHub-style) |
| Maintenance timeline | service_records | Gantt-style per vehicle |
| Medication adherence | medication taken logs | Weekly percentage bar chart |
| Financial snapshot | account balances monthly | Stacked bar: assets vs liabilities |
| Lab value trends | time_series_metrics (cholesterol, A1C, etc.) | Line with reference range shading |
| Pet weight | time_series_metrics (pet_weight) per pet | Line chart |

#### 9.3 Dashboard Widgets

Embeddable chart components for each domain dashboard:
- Medical dashboard: weight sparkline, BP sparkline, adherence donut
- Financial dashboard: debt paydown progress bar, net worth trend
- Auto dashboard: cost-per-mile over time, maintenance cost by category
- Vet dashboard: per-pet weight sparklines

#### 9.4 Monthly/Quarterly Reports

On-demand or scheduled report generation:
```
GET /api/reports/monthly?year=2025&month=3
GET /api/reports/quarterly?year=2025&quarter=1
```

Returns comprehensive markdown or PDF summarizing:
- Health metrics and trends
- Financial changes (debt reduction, spending patterns)
- Maintenance completed and upcoming
- Documents ingested and actions resolved
- Key stats: documents stored, metrics tracked, actions completed

### Testing Checklist — Phase 9

- [ ] Weight trend chart renders with correct data points
- [ ] BP chart shows dual lines with normal range shading
- [ ] Debt paydown chart updates when balances change
- [ ] Workout heatmap accurately reflects completed workouts
- [ ] Dashboard sparklines load without performance issues
- [ ] Trend calculations correct (direction, rate, projected goal date)
- [ ] Charts handle missing data gracefully (gaps, sparse data)
- [ ] Monthly report generates with accurate data
- [ ] Charts are responsive on mobile
- [ ] Date range filters work correctly

---

## Phase 10: Google Calendar Integration

### Goal
Sync actionable items from LifeOS to Google Calendar so Dave sees upcoming maintenance, appointments, renewals, and deadlines alongside his regular calendar.

### Deliverables

#### 10.1 Google Calendar OAuth Setup

**Uses existing Google Cloud project:** `iconic-monitor-489123-s4`

**Scopes needed:** `https://www.googleapis.com/auth/calendar.events`

**Setup steps:**
1. Enable Google Calendar API in the existing project
2. Create OAuth credentials (or reuse existing if scopes allow)
3. Run OAuth flow to obtain refresh token
4. Store credentials in `/srv/lifeos/auth/google-calendar-tokens.json`

**Dedicated calendar:** Create a "LifeOS" calendar in Dave's Google account. All LifeOS events go here — keeps them visually distinct from personal/work events.

#### 10.2 Calendar Sync Service

**File:** `api/calendar_sync.py`

**Sync behavior:**
- When an action_item is created with a due_date → create Google Calendar event
- When an action_item is updated (rescheduled, completed, dismissed) → update/delete calendar event
- Store `calendar_event_id` on the action_item for bidirectional tracking
- Recurring action items (oil change every 6 months, annual vet visit) → create recurring calendar events with RRULE

**Event format:**
```
Title: [LifeOS] Oil Change — 2023 Tacoma
When: 2025-07-15 (all-day event)
Description: |
  Service Type: Oil Change
  Vehicle: 2023 Toyota Tacoma (31,000 mi)
  Provider: Toyota of Castle Rock
  Est. Cost: $75
  
  Logged by LifeOS — https://lifeos.davidcol.es/actions/{id}
Location: Toyota of Castle Rock, 123 Auto Dr, Castle Rock, CO
```

**Sync frequency:** Real-time on action_item create/update. Daily reconciliation job to catch any drift.

#### 10.3 Event Types to Sync

| Action Type | Calendar Behavior |
|------------|------------------|
| Appointment (medical, vet) | Timed event at appointment time |
| Payment due date | All-day event on due date |
| Maintenance due | All-day event on estimated due date |
| Document expiration | All-day event 30 days before expiration |
| Prescription refill | All-day event 7 days before refill date |
| Registration/license renewal | All-day event 60 days before expiration |
| Tax deadline | All-day event on deadline + reminder 14 days before |

#### 10.4 Settings

User-configurable:
- Which domains sync to calendar (default: all)
- Which action types sync (default: all)
- Reminder lead time per type (e.g., 60 days for registration, 7 days for refills)
- Calendar ID (in case Dave wants a different calendar)

### Testing Checklist — Phase 10

- [ ] Google Calendar OAuth flow completes successfully
- [ ] "LifeOS" calendar created in Google Calendar
- [ ] Action item with due date → calendar event created
- [ ] Action item completed → calendar event deleted
- [ ] Action item rescheduled → calendar event updated
- [ ] Recurring action item → recurring calendar event with correct RRULE
- [ ] Event description includes relevant details and LifeOS link
- [ ] Events display correctly in Google Calendar (web and mobile)
- [ ] Daily reconciliation catches any orphaned events
- [ ] Settings: disable sync for specific domain → events not created
- [ ] No duplicate events on repeated sync

---

## Phase 11: Insurance & Legal Modules

### Goal
Cross-cutting insurance policy tracking and identity document management. Insurance touches every domain (health, auto, home, pet) — this phase centralizes policy tracking with links to the relevant domain.

### Deliverables

#### 11.1 Insurance Structured Record Types

**`insurance_policy`:**
```json
{
  "carrier": "State Farm",
  "policy_number": "POL-123456",
  "policy_type": "auto",
  "coverage_type": "full",
  "premium_monthly": 145,
  "premium_frequency": "monthly",
  "deductible": 500,
  "coverage_limits": {
    "bodily_injury": "100/300",
    "property_damage": 100000,
    "comprehensive": 500,
    "collision": 500
  },
  "effective_date": "2025-01-01",
  "expiration_date": "2025-07-01",
  "auto_renew": true,
  "agent_name": "John Smith",
  "agent_phone": "303-555-0300",
  "linked_domain": "auto",
  "linked_record_id": "vehicle-uuid-here",
  "notes": "Bundled with home for 15% discount"
}
```

**Types:** auto, home, health, dental, vision, life, pet, umbrella, disability, renters

#### 11.2 Legal / Identity Record Types

**`identity_document`:**
```json
{
  "document_type": "passport",
  "issuing_authority": "US Department of State",
  "document_number_last4": "5678",
  "issue_date": "2020-06-15",
  "expiration_date": "2030-06-14",
  "notes": "Stored in home safe"
}
```

**Types:** passport, drivers_license, birth_certificate, marriage_certificate, social_security_card, vehicle_title, property_deed, will, trust, power_of_attorney

**`legal_contact`:**
```json
{
  "name": "Jane Doe, Esq.",
  "specialty": "estate planning",
  "firm": "Doe & Associates",
  "phone": "303-555-0400",
  "email": "jane@doelaw.com",
  "last_consulted": "2024-11-15",
  "notes": "Drafted will and trust"
}
```

#### 11.3 Insurance Dashboard

- Policy inventory: all active policies with renewal dates, premiums
- Premium calendar: when payments are due
- Coverage gaps: highlight domains without coverage
- Renewal timeline: policies expiring in next 90 days
- Claims history (if logged)

#### 11.4 Legal Dashboard

- Identity documents: expiration tracker
- Estate planning status: will, trust, POA — last reviewed dates
- Legal contacts directory

### Testing Checklist — Phase 11

- [ ] Create insurance policy → linked to correct domain record (vehicle, property, pet)
- [ ] Policy expiration → action item created with configurable lead time
- [ ] Upload insurance declaration page → AI extracts policy details
- [ ] Identity document expiration tracking (passport, license)
- [ ] Insurance dashboard shows all policies with renewal timeline
- [ ] Premium totals calculated correctly (monthly obligation)
- [ ] Coverage gap detection works (e.g., no renter's insurance flagged)
- [ ] Legal contacts searchable by specialty

---

## Phase 12: Polish & Hardening

### Goal
Production-readiness. Performance optimization, comprehensive error handling, backup verification, security audit, and UX refinement based on real usage.

### Deliverables

#### 12.1 Performance Optimization

- **Database indexing audit:** EXPLAIN ANALYZE on all common queries, add missing indexes
- **Embedding batch processing:** Queue multiple uploads for batch embedding generation
- **Query caching:** Cache frequently-accessed aggregations (debt totals, trend computations) with TTL
- **Frontend lazy loading:** Domain dashboards load on demand, not all at once
- **Image optimization:** Thumbnails for document previews, lazy-loaded full images

#### 12.2 Error Handling & Resilience

- **AI analysis fallback:** If Claude API is unreachable, store document without analysis, queue for retry
- **OCR failure handling:** If OCR fails, store the raw file with a "needs OCR" flag
- **Graceful degradation:** System remains usable (upload, search existing docs) even if Claude API is down
- **Rate limiting:** Protect against accidental bulk operations
- **Input validation:** Comprehensive validation on all API inputs (especially JSONB structured records)

#### 12.3 Backup Verification

Extend Ezekiel's backup architecture to cover LifeOS:
- **PostgreSQL dump** (all tables including structured records, metrics, action items)
- **Qdrant snapshot** (all embeddings)
- **Document files** (`/srv/lifeos/documents/`)
- **Auth tokens** (`/srv/lifeos/auth/`)
- **Automated restore test:** Monthly cron that restores to a test database and verifies record counts

#### 12.4 Security Audit

- [ ] Cloudflare Access enforced on all routes
- [ ] Agent API keys validated and scoped correctly
- [ ] No PII in logs (mask document content in error logs)
- [ ] Health data encrypted at rest (column-level encryption on sensitive medical fields)
- [ ] File upload validation (size limits, type checking, virus scan if feasible)
- [ ] CORS configuration locked down
- [ ] Rate limiting on AI Q&A endpoint (prevent runaway Claude API costs)

#### 12.5 UX Refinement

Based on actual usage patterns (tracked in audit_log):
- **Shortcuts:** Most common actions should be 1-2 clicks from home screen
- **Search improvements:** Fuzzy matching, search suggestions, recent searches
- **Bulk operations:** Select multiple documents for re-classification or deletion
- **Keyboard shortcuts:** For power users (Cmd+K for search, etc.)
- **Notification preferences:** Email digest of upcoming actions (daily or weekly)
- **Dark mode:** Because it's 2026

#### 12.6 Documentation

- **CLAUDE.md:** Comprehensive technical reference for Claude Code maintenance
- **User guide:** How to use each feature (written for Dave, not developers)
- **API documentation:** OpenAPI/Swagger spec for all endpoints
- **Agent integration guide:** How to connect new OpenClaw agents to LifeOS

### Testing Checklist — Phase 12

- [ ] Load test: 1000+ documents in system, search responds < 2 seconds
- [ ] Load test: 10,000+ time series data points, trend chart renders < 3 seconds
- [ ] Backup runs successfully and restore test passes
- [ ] Claude API outage: system degrades gracefully (upload works, search works, AI features show "unavailable")
- [ ] Invalid file upload handled gracefully (corrupt PDF, unsupported format)
- [ ] Agent API key with wrong scope returns 403
- [ ] All endpoints return appropriate error responses (not stack traces)
- [ ] Mobile UI is usable for all core workflows
- [ ] Dark mode renders correctly across all views

---

## Appendix A: Document Category Taxonomy

### Medical
`lab_result`, `visit_note`, `prescription`, `referral`, `imaging_report`, `surgical_report`, `discharge_summary`, `vaccination_record`, `insurance_eob`, `insurance_claim`, `dental_record`, `vision_record`, `therapy_note`, `medical_bill`, `prior_authorization`, `health_summary`, `advance_directive`

### Financial
`tax_return`, `w2`, `1099`, `bank_statement`, `credit_card_statement`, `loan_agreement`, `mortgage_statement`, `investment_statement`, `receipt`, `invoice`, `pay_stub`, `financial_plan`, `budget`, `credit_report`, `tax_estimate`

### Auto
`registration`, `title`, `insurance_card`, `service_receipt`, `recall_notice`, `purchase_agreement`, `lease_agreement`, `inspection_report`, `warranty`, `owners_manual`

### Home
`mortgage_agreement`, `lease`, `hoa_document`, `insurance_policy`, `warranty`, `contractor_invoice`, `permit`, `inspection_report`, `appraisal`, `property_tax`, `utility_bill`, `home_improvement_receipt`

### Veterinary
`vet_visit_note`, `vaccination_record`, `prescription`, `lab_result`, `surgical_report`, `boarding_record`, `pet_insurance_claim`, `adoption_paper`, `registration`, `microchip_record`, `dental_record`

### Legal
`passport`, `drivers_license`, `birth_certificate`, `marriage_certificate`, `social_security_card`, `will`, `trust_document`, `power_of_attorney`, `court_document`, `contract`, `notarized_document`

### Insurance
`policy_declaration`, `premium_notice`, `claim`, `eob`, `coverage_summary`, `renewal_notice`, `cancellation_notice`, `agent_correspondence`

---

## Appendix B: Environment Variables

```bash
# Database
POSTGRES_PASSWORD=             # Strong random password
POSTGRES_USER=lifeos
POSTGRES_DB=lifeos

# API
SECRET_KEY=                    # JWT signing key (generate with: openssl rand -hex 32)
ANTHROPIC_API_KEY=             # For AI analysis and Q&A
DATA_PATH=/srv/lifeos          # Root path for all persistent data
API_PORT=8000

# Email Ingestion (Phase 3)
EMAIL_IMAP_HOST=imap.gmail.com
EMAIL_IMAP_USER=docs@davidcol.es
EMAIL_IMAP_PASSWORD=           # App-specific password
EMAIL_POLL_INTERVAL=120        # Seconds between IMAP polls

# Google Calendar (Phase 10)
GOOGLE_CALENDAR_ID=            # "LifeOS" calendar ID
GOOGLE_CREDENTIALS_PATH=/srv/lifeos/auth/google-calendar-tokens.json

# Cloudflare (optional, for external access)
TUNNEL_TOKEN=                  # Cloudflare Tunnel token

# Agent API Keys (generated per agent)
AGENT_KEY_HEALTHBOT=           # Scoped to medical domain
AGENT_KEY_FINANCEBOT=          # Scoped to financial domain
AGENT_KEY_MEALBOT=             # Scoped to read-only health preferences

# Encryption
HEALTH_DATA_ENCRYPTION_KEY=    # For medical data at rest (generate with: openssl rand -hex 32)
```

---

## Appendix C: Deployment Quick Reference

```bash
# Initial setup
git clone git@github.com:YOUR_USERNAME/lifeos.git /opt/lifeos
cd /opt/lifeos

# Create data directories
sudo mkdir -p /srv/lifeos/{postgres,qdrant,documents,backups,auth}
sudo mkdir -p /srv/lifeos/documents/{files,scans,import,attachments}
sudo chown -R $USER:$USER /srv/lifeos

# Configure
cp .env.example .env
# Edit .env with passwords, API keys, tokens

# Launch
docker compose up -d --build

# Verify
docker compose ps
curl http://localhost:8000/api/health
curl http://localhost:8000/api/stats

# First run: seed primary subject
curl -X POST http://localhost:8000/api/subjects \
  -H "Content-Type: application/json" \
  -d '{"name": "Dave", "type": "person", "is_primary": true}'
```

---

## Appendix D: Project File Structure

```
lifeos/
├── api/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py                 # FastAPI app entry point
│   ├── config.py               # Settings from environment
│   ├── init_db.sql             # PostgreSQL schema (all tables)
│   ├── processor.py            # OCR + text extraction pipeline
│   ├── ai_analyzer.py          # Claude-powered document analysis (Phase 2)
│   ├── search.py               # Qdrant vector + PostgreSQL full-text search
│   ├── calendar_sync.py        # Google Calendar integration (Phase 10)
│   ├── email_ingest.py         # Email forwarding processor (Phase 3)
│   ├── agent_auth.py           # Agent API key validation + scoping
│   ├── schemas/                # JSON schemas for structured record types
│   │   ├── medical.py
│   │   ├── financial.py
│   │   ├── auto.py
│   │   ├── home.py
│   │   ├── vet.py
│   │   ├── insurance.py
│   │   └── legal.py
│   ├── routers/                # FastAPI route modules
│   │   ├── documents.py
│   │   ├── search.py
│   │   ├── subjects.py
│   │   ├── actions.py
│   │   ├── trends.py
│   │   ├── agent_health.py
│   │   ├── agent_finance.py
│   │   └── reports.py
│   └── scripts/
│       ├── reindex.py
│       ├── import_attachments.py
│       └── backup.sh
├── nginx/
│   └── nginx.conf
├── web/
│   └── dist/
│       ├── index.html          # Main SPA
│       ├── capture.html        # Mobile capture PWA (Phase 4)
│       ├── manifest.json       # PWA manifest
│       └── sw.js               # Service worker
├── docker-compose.yml
├── .env.example
├── CLAUDE.md                   # Technical reference for Claude Code
└── README.md
```
