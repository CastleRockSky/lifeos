# LifeOS — Phases 4–8: Mobile Capture & Domain Modules

---

## Phase 4: Mobile Capture

### Goal
A lightweight, camera-first mobile interface for scanning documents on the go. At the vet clinic, at a car service center, picking up a prescription — pull out your phone, snap a photo, and it's in the system.

### Deliverables

#### 4.1 Progressive Web App (PWA)

**File:** `web/dist/capture.html` (separate entry point, optimized for mobile)

A minimal PWA that:
- Opens camera directly on launch (with a "capture" button)
- Supports multi-page capture (take 3 photos of a 3-page document → merged into one document)
- Shows capture preview with retake option
- One-tap upload with optional domain hint (dropdown: Medical, Financial, Auto, Vet, Other)
- Upload progress indicator
- "Processing..." status until AI analysis completes
- Success confirmation with link to view in full UI

**PWA requirements:**
- Service worker for offline queueing (photos captured offline upload when connectivity returns)
- Manifest file for "Add to Home Screen" on iOS/Android
- Camera access via `getUserMedia` API
- Image compression before upload (target: < 2MB per image, quality sufficient for OCR)

#### 4.2 Multi-Page Document Assembly

**API endpoint:** `POST /api/documents/upload-multi`

Accepts multiple images as a single document:
1. Receive N images
2. Order by upload sequence
3. Run OCR on each image
4. Concatenate extracted text in page order
5. Store all images as pages of one document
6. Run AI analysis on the combined text

#### 4.3 Capture History

Mobile UI shows recent captures with processing status:
- 📤 Uploading...
- 🔄 Processing...
- ✅ Filed as: "Quest Diagnostics — Lipid Panel" (Medical)
- ⚠️ Needs review (low confidence)

### Testing Checklist — Phase 4

- [ ] PWA installs on iOS Safari ("Add to Home Screen")
- [ ] PWA installs on Android Chrome
- [ ] Camera opens directly on launch
- [ ] Single photo capture → uploads, OCR, AI analysis
- [ ] Multi-page capture (3 photos) → merged into single document
- [ ] Domain hint dropdown works and improves AI classification
- [ ] Offline capture queues and uploads when connection returns
- [ ] Image compression keeps file size manageable
- [ ] Capture history shows real-time processing status
- [ ] OCR quality is sufficient from phone camera photos (test in various lighting)

---

## Phase 5: Medical Module

### Goal
Full structured data support for health management. This phase builds the medical-specific data model, HealthBot API integration, and health dashboard. It replaces HealthBot's workspace files (METRICS.md, MED_LOG.md, etc.) with a proper database while keeping HealthBot as the conversational interface.

### Deliverables

#### 5.1 Medical Structured Record Types

Define JSON schemas for these `structured_records.record_type` values:

**`provider`:**
```json
{
  "name": "Dr. Sarah Chen",
  "specialty": "Internal Medicine",
  "practice": "Castle Rock Medical Group",
  "phone": "303-555-0100",
  "fax": "303-555-0101",
  "portal_url": "https://mychart.example.com",
  "address": "123 Medical Dr, Castle Rock, CO 80104",
  "npi": "1234567890",
  "next_appointment": "2025-06-15",
  "notes": "Preferred PCP since 2023"
}
```

**`medication`:**
```json
{
  "name": "Lisinopril",
  "dose": "10mg",
  "frequency": "1x daily",
  "time_of_day": "morning",
  "prescriber": "Dr. Sarah Chen",
  "pharmacy": "King Soopers #123",
  "rx_number": "RX-7891234",
  "start_date": "2024-01-15",
  "refill_date": "2025-04-01",
  "quantity": 90,
  "refills_remaining": 3,
  "indication": "Hypertension",
  "notes": "Take on empty stomach"
}
```

**`condition`:**
```json
{
  "name": "Essential Hypertension",
  "icd10": "I10",
  "diagnosed_date": "2024-01-15",
  "diagnosing_provider": "Dr. Sarah Chen",
  "status": "active",
  "management": "Medication + lifestyle",
  "notes": "Well-controlled with Lisinopril"
}
```

**`vaccination`:**
```json
{
  "name": "Influenza",
  "date_administered": "2024-10-15",
  "provider": "Walgreens",
  "lot_number": "FL2024-789",
  "next_due": "2025-10-01",
  "series": null,
  "dose_number": null
}
```

**`lab_result_set`:**
```json
{
  "lab": "Quest Diagnostics",
  "ordering_provider": "Dr. Sarah Chen",
  "date": "2025-03-15",
  "results": [
    {
      "test": "Total Cholesterol",
      "value": 210,
      "unit": "mg/dL",
      "reference_range": "< 200",
      "flag": "high"
    },
    {
      "test": "HDL",
      "value": 55,
      "unit": "mg/dL",
      "reference_range": "> 40",
      "flag": "normal"
    }
  ]
}
```

#### 5.2 Health Metric Types

Predefined `time_series_metrics.metric_type` values for medical:

| Metric Type | Value Type | Unit | Notes |
|------------|------------|------|-------|
| `weight` | numeric | lbs | Daily weigh-in |
| `blood_pressure_systolic` | numeric | mmHg | Paired with diastolic |
| `blood_pressure_diastolic` | numeric | mmHg | Paired with systolic |
| `heart_rate_resting` | numeric | bpm | |
| `blood_glucose` | numeric | mg/dL | |
| `cholesterol_total` | numeric | mg/dL | From lab results |
| `cholesterol_hdl` | numeric | mg/dL | From lab results |
| `cholesterol_ldl` | numeric | mg/dL | From lab results |
| `a1c` | numeric | % | From lab results |
| `waist_circumference` | numeric | inches | |
| `body_fat_pct` | numeric | % | |
| `energy_level` | numeric | 1-10 scale | Subjective daily rating |
| `sleep_hours` | numeric | hours | |

#### 5.3 HealthBot Agent API

**Routes (all require `X-Agent-Key: healthbot-api-key`):**

```
# Metrics
GET    /api/agent/health/metrics?subject=dave&type=weight&days=30
POST   /api/agent/health/metrics
       Body: {"subject": "dave", "type": "weight", "value": 185.4, "notes": "morning weigh-in"}

# Blood pressure (convenience endpoint — logs both systolic and diastolic)
POST   /api/agent/health/bp
       Body: {"subject": "dave", "systolic": 130, "diastolic": 82, "pulse": 68, "notes": "morning reading"}

# Medications
GET    /api/agent/health/medications?subject=dave&active=true
POST   /api/agent/health/medications/{id}/taken
       Body: {"timestamp": "2025-03-27T07:15:00", "status": "taken"}  // or "missed", "late"

# Medication adherence
GET    /api/agent/health/medications/adherence?subject=dave&days=30

# Providers
GET    /api/agent/health/providers?subject=dave

# Trends (pre-computed for agent consumption)
GET    /api/agent/health/trends?subject=dave&type=weight&period=weekly
       Returns: {"current_avg": 184.2, "prior_avg": 185.8, "direction": "down", "change": -1.6}
```

#### 5.4 Medical Dashboard

**UI components:**
- **Health card:** Current weight (with trend arrow), latest BP, medication adherence %
- **Providers list:** Name, specialty, phone, next appointment — click to expand
- **Medications:** Active meds with refill countdown
- **Recent labs:** Flagged values highlighted, trend sparklines
- **Action items:** Upcoming appointments, refills due, follow-ups needed

### Testing Checklist — Phase 5

- [ ] Create provider record → appears in providers list
- [ ] Create medication record → appears in active medications
- [ ] HealthBot API: log weight via POST → stored in time_series_metrics
- [ ] HealthBot API: log BP via convenience endpoint → both systolic and diastolic stored
- [ ] HealthBot API: confirm medication taken → adherence logged
- [ ] HealthBot API: get trends → correct weekly averages and direction
- [ ] Upload a lab result PDF → AI extracts lab values into structured records and metrics
- [ ] Upload a prescription → AI creates/updates medication record
- [ ] Medication refill date creates action item
- [ ] Medical dashboard displays all components correctly
- [ ] Health data is NOT accessible via other agent API keys (scope enforcement)

---

## Phase 6: Financial Module

### Goal
Structured financial data tracking — accounts, debts, recurring expenses, and tax timeline management. Gives Dave (and FinanceBot) a clear picture of financial obligations and upcoming deadlines.

### Deliverables

#### 6.1 Financial Structured Record Types

**`bank_account`:**
```json
{
  "institution": "Chase",
  "account_type": "checking",
  "last_four": "4567",
  "balance": 5432.10,
  "balance_date": "2025-03-27",
  "monthly_fee": 0,
  "notes": "Primary business checking"
}
```

**`credit_account`:**
```json
{
  "creditor": "Chase Sapphire",
  "last_four": "8901",
  "credit_limit": 15000,
  "current_balance": 3200.50,
  "balance_date": "2025-03-27",
  "apr": 24.99,
  "minimum_payment": 75,
  "payment_due_date": "2025-04-15",
  "autopay": true,
  "autopay_amount": "minimum",
  "notes": "Business expenses card"
}
```

**`loan`:**
```json
{
  "lender": "US Bank",
  "loan_type": "auto",
  "original_amount": 28000,
  "current_balance": 18500,
  "balance_date": "2025-03-27",
  "interest_rate": 5.9,
  "monthly_payment": 485,
  "payment_due_day": 15,
  "remaining_payments": 42,
  "payoff_date": "2028-09-15",
  "collateral": "2023 Toyota Tacoma",
  "autopay": true,
  "notes": ""
}
```

**`recurring_expense`:**
```json
{
  "name": "Comcast Internet",
  "amount": 89.99,
  "frequency": "monthly",
  "due_day": 22,
  "category": "utilities",
  "autopay": true,
  "account": "Chase Checking *4567",
  "notes": "Contract through Dec 2025"
}
```

**`tax_item`:**
```json
{
  "tax_year": 2025,
  "item_type": "deadline",
  "description": "Q1 Estimated Tax Payment",
  "due_date": "2025-04-15",
  "amount": 2500,
  "status": "pending",
  "notes": "Federal + Colorado state"
}
```

#### 6.2 Financial Metric Types

| Metric Type | Value Type | Unit | Notes |
|------------|------------|------|-------|
| `net_worth` | numeric | USD | Monthly snapshot |
| `total_debt` | numeric | USD | Sum of all debts |
| `total_credit_utilization` | numeric | % | Aggregate across cards |
| `monthly_obligations` | numeric | USD | Sum of all recurring payments |

#### 6.3 FinanceBot Agent API

```
GET    /api/agent/finance/summary
       Returns: total debt, monthly obligations, upcoming payments (7 days)

GET    /api/agent/finance/debts?type=all|credit|loan
       Returns: list of debts with balances, rates, minimums

GET    /api/agent/finance/accounts?type=checking|savings|credit|investment

GET    /api/agent/finance/upcoming-payments?days=14

POST   /api/agent/finance/balance-update
       Body: {"record_id": "...", "balance": 3100.50}
       For manual balance updates via agent
```

#### 6.4 Financial Dashboard

- **Debt overview:** Total debt, breakdown by type, minimum monthly obligations
- **Accounts summary:** Balances across all accounts
- **Upcoming payments:** Next 14 days, with autopay indicators
- **Tax timeline:** Upcoming deadlines for current tax year
- **Debt paydown tracker:** Visual progress toward zero (if goal set)

### Testing Checklist — Phase 6

- [ ] Create credit account record → appears in accounts list
- [ ] Create loan record → payment due date generates recurring action items
- [ ] Create recurring expense → monthly reminders generated
- [ ] FinanceBot API: get debt summary → accurate totals
- [ ] FinanceBot API: get upcoming payments → correct items and dates
- [ ] Upload a credit card statement → AI extracts balance, payment due
- [ ] Upload a loan document → AI creates loan record
- [ ] Financial dashboard displays all components correctly
- [ ] Tax timeline shows upcoming deadlines
- [ ] Financial data is NOT accessible via HealthBot API key (scope enforcement)

---

## Phase 7: Auto & Home Modules

### Goal
Track vehicles and property with maintenance schedules, service history, warranty tracking, and contractor management. The value here is proactive maintenance reminders — "oil change due in 500 miles" or "furnace filter replacement overdue."

### Deliverables

#### 7.1 Auto Structured Record Types

**`vehicle`:**
```json
{
  "year": 2023,
  "make": "Toyota",
  "model": "Tacoma",
  "trim": "TRD Off-Road",
  "vin": "JTXXX...",
  "license_plate": "ABC-1234",
  "color": "Lunar Rock",
  "purchase_date": "2023-06-15",
  "purchase_price": 42000,
  "current_mileage": 28500,
  "mileage_updated": "2025-03-15",
  "registration_expiration": "2025-12-31",
  "insurance_policy_id": null,
  "loan_record_id": null,
  "notes": ""
}
```

**`maintenance_schedule`:**
```json
{
  "vehicle_record_id": "...",
  "service_type": "Oil Change",
  "interval_miles": 5000,
  "interval_months": 6,
  "last_service_date": "2025-01-15",
  "last_service_mileage": 26000,
  "next_due_date": "2025-07-15",
  "next_due_mileage": 31000,
  "estimated_cost": 75,
  "provider": "Toyota of Castle Rock",
  "notes": "Full synthetic 0W-20"
}
```

**`service_record`:**
```json
{
  "vehicle_record_id": "...",
  "date": "2025-01-15",
  "mileage": 26000,
  "service_type": "Oil Change",
  "provider": "Toyota of Castle Rock",
  "cost": 72.50,
  "parts": ["Oil filter", "5qt 0W-20 synthetic"],
  "notes": "Tire rotation included",
  "document_id": null
}
```

#### 7.2 Home Structured Record Types

**`property`:**
```json
{
  "address": "123 Main St, Castle Rock, CO 80104",
  "type": "single_family",
  "year_built": 2018,
  "sqft": 2400,
  "bedrooms": 4,
  "bathrooms": 3,
  "hoa": true,
  "hoa_monthly": 75,
  "mortgage_record_id": null,
  "insurance_policy_id": null,
  "notes": ""
}
```

**`appliance`:**
```json
{
  "name": "HVAC System",
  "brand": "Lennox",
  "model": "XC21",
  "serial": "XXXX",
  "install_date": "2018-03-15",
  "warranty_expiration": "2028-03-15",
  "last_service": "2024-10-15",
  "service_interval_months": 12,
  "next_service_due": "2025-10-15",
  "contractor_record_id": null,
  "notes": "Filter size: 20x25x4, replace every 3 months"
}
```

**`contractor`:**
```json
{
  "name": "Mike's Plumbing",
  "trade": "plumbing",
  "phone": "303-555-0200",
  "email": "mike@mikesplumbing.com",
  "rating": 5,
  "last_used": "2024-08-10",
  "notes": "Great work, fair pricing. Used for water heater install."
}
```

**`home_maintenance_schedule`:**
```json
{
  "task": "Replace HVAC filter",
  "interval_months": 3,
  "last_completed": "2025-01-15",
  "next_due": "2025-04-15",
  "estimated_cost": 25,
  "diy": true,
  "notes": "20x25x4 MERV 11 from Amazon"
}
```

#### 7.3 Mileage Logging

Quick mileage update via API or UI:
```
POST /api/vehicles/{id}/mileage
Body: {"mileage": 29000, "date": "2025-03-27"}
```

When mileage is logged, system checks all maintenance_schedule records for that vehicle and updates `next_due_mileage` calculations. If service is due within 500 miles or past due, creates/updates action item.

#### 7.4 Auto & Home Dashboards

**Auto:**
- Vehicle cards: photo (if uploaded), year/make/model, current mileage
- Maintenance timeline: upcoming service items with countdown (miles or date)
- Registration/insurance expiration alerts
- Service history log

**Home:**
- Property overview
- Warranty tracker: items sorted by expiration date, expired items highlighted
- Maintenance calendar: seasonal and recurring tasks
- Contractor directory: searchable by trade

### Testing Checklist — Phase 7

- [ ] Create vehicle record → displays on auto dashboard
- [ ] Create maintenance schedule → generates action items at correct intervals
- [ ] Log mileage update → maintenance due calculations update
- [ ] Upload service receipt → AI links to vehicle, creates service record
- [ ] Registration expiration → action item created 60 days before
- [ ] Create appliance record → warranty expiration tracked
- [ ] Create home maintenance schedule → recurring action items generated
- [ ] Upload contractor invoice → AI extracts cost, links to property
- [ ] Contractor directory searchable by trade
- [ ] Warranty expiration alerts display correctly

---

## Phase 8: Veterinary & Pets Module

### Goal
Per-pet health tracking using the same patterns established in Phase 5 (Medical). Vaccination schedules, medication tracking, vet providers, weight trends — everything you need at the vet's office or when boarding your pet.

### Deliverables

#### 8.1 Pet Subject Profile

Pets are `subjects` with `type = "pet"` and `profile_data`:

```json
{
  "species": "dog",
  "breed": "Golden Retriever",
  "date_of_birth": "2020-04-15",
  "sex": "male",
  "neutered": true,
  "color": "golden",
  "weight_current": 72,
  "weight_updated": "2025-03-15",
  "microchip": "985141234567890",
  "license_number": "DR-2024-1234",
  "license_expiration": "2025-12-31",
  "insurance_policy_id": null,
  "adoption_date": "2020-06-01",
  "dietary_needs": "Large breed adult formula",
  "allergies": ["chicken"],
  "notes": ""
}
```

#### 8.2 Pet-Specific Record Types

Reuses medical patterns with pet-specific additions:

- **`vet_provider`** — same schema as `provider` with `species_specialty` field
- **`pet_medication`** — same schema as `medication` with `weight_based_dosing` field
- **`pet_vaccination`** — same schema as `vaccination` with `required_for_boarding` flag
- **`pet_condition`** — same as `condition`
- **`preventative_schedule`** — flea/tick, heartworm, dental cleaning intervals

```json
{
  "type": "flea_tick",
  "product": "NexGard",
  "dose": "68mg",
  "frequency": "monthly",
  "last_administered": "2025-03-01",
  "next_due": "2025-04-01",
  "cost_per_dose": 42,
  "notes": "Chewable — give with food"
}
```

#### 8.3 Pet Health Metrics

| Metric Type | Notes |
|------------|-------|
| `pet_weight` | Track at every vet visit and periodically at home |
| `pet_temperature` | If checked at vet |
| `pet_body_condition_score` | 1-9 scale |

#### 8.4 Pet Health Card View

A single-page summary designed to be useful at the vet or boarding facility:
- Pet photo + basic info (breed, age, weight, microchip)
- Current medications
- Vaccination status (with dates and next due)
- Allergies / dietary needs
- Conditions
- Primary vet info

This view should be printable / shareable as PDF.

#### 8.5 Multi-Pet Support

All queries are per-subject. Dashboard shows a pet selector with individual health cards. Quick-switch between pets in the UI.

### Testing Checklist — Phase 8

- [ ] Create pet subject → appears in pets list
- [ ] Create vet provider record → linked to pet
- [ ] Create vaccination record → next due date generates action item
- [ ] Upload vet visit notes → AI identifies pet by name, extracts data
- [ ] Preventative schedule (flea/tick monthly) → recurring action items
- [ ] Pet health card displays complete, accurate info
- [ ] Pet health card exportable as PDF
- [ ] Weight tracking over time with trend display
- [ ] Multiple pets supported with independent tracking
- [ ] Pet license expiration → action item created
