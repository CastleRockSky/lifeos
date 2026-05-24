# Auto Page — Current State

> Snapshot of the LifeOS Auto page as it exists today, written for a
> design/UX discussion. Captures what's on screen, what data it pulls from,
> what isn't pulled but exists in the data model, and the rough seams where
> a redesign would land.

---

## At-a-glance

The Auto page is a flat, vertical list rendered from `<template x-if="view === 'auto'">` in `web/dist/index.html` (≈ line 1660–1753). It shows three concatenated sections in a single scroll:

1. **Vehicles** — one row per vehicle with inline mileage logging.
2. **Upcoming maintenance** — pending action items in the `auto` domain.
3. **Service history** — recent `service_record` entries with a link to the source document.

There is **no per-vehicle drill-in page**, no tabs, no charts, no filters, and no UI affordance for creating, editing, deleting, or merging vehicles, schedules, or services. Everything that exists on the page is read-only except mileage logging.

---

## Section 1: Vehicles

For each `structured_records` row where `record_type='vehicle'`:

**Shown:**
- `year make model` (line 1)
- Optional `trim` (smaller, after the model)
- `current_mileage` (with thousands separator) — e.g. `45,210 mi`
- `license_plate`
- `color`
- `registration_expiration` — highlighted with `badge-priority-high` if within 60 days

**Inline action:**
- Numeric input (placeholder shows last logged mileage)
- "Log" button → `POST /api/vehicles/{id}/mileage`. That endpoint:
  - Updates `current_mileage` on the vehicle record
  - Writes a `time_series_metrics` row (`metric_type='mileage'`)
  - Recomputes `next_due_mileage` on every related `maintenance_schedule`
  - Creates `action_items` for any service due within 500 miles (or overdue)
  - Calls `ensure_recurring_action_item` for date-based schedules

**Not shown** (but stored on the vehicle record per `api/schemas/auto.py`):
- VIN
- Purchase date / purchase price
- Mileage-updated date
- Insurance policy link (`insurance_policy_id`)
- Loan record link (`loan_record_id`)
- Status (active / sold / totaled)
- Free-text notes

Empty state: "No vehicles on file yet. Upload a registration or insurance card to get started."

---

## Section 2: Upcoming maintenance

For each row from `GET /api/actions?domain=auto&status=pending&per_page=20`:

**Shown:**
- `title`
- `due_date` (formatted)
- `description` (e.g. "Due in 320 miles (current 44,680, due 45,000)")
- `priority` badge
- "Done" button → marks action complete

Row gets the `overdue` class when `due_date` is in the past.

**Not shown:**
- Which vehicle the action applies to (you can infer from the description text only)
- Estimated cost from the schedule (`estimated_cost` field exists)
- Preferred provider (`provider` field exists)

---

## Section 3: Service history

For each `service_record` (capped to 20 most recent):

**Shown:**
- `service_type`
- `date`
- `mileage`
- `provider`
- `cost`
- "Doc" link to the source PDF when `source_document_id` is set

**Not shown:**
- Which vehicle (no grouping)
- Parts list (`parts: list[str]` exists on the schema)
- Free-text notes
- Running totals (cost-per-year, cost-per-mile, total service spend)

---

## Data sources (everything the Auto page touches)

```
GET /api/records?record_type=vehicle               → Section 1
GET /api/records?record_type=maintenance_schedule  → loaded but UNUSED on this page
GET /api/records?record_type=service_record        → Section 3 (per_page=20)
GET /api/actions?domain=auto&status=pending        → Section 2 (per_page=20)
POST /api/vehicles/{id}/mileage                    → Log button
```

The `maintenance_schedule` records are fetched but never rendered. The schedule data only shows up indirectly, as action items the schedule produced.

---

## Underlying schemas (`api/schemas/auto.py`)

```python
Vehicle:
    year, make, model, trim, vin, license_plate, color,
    purchase_date, purchase_price,
    current_mileage, mileage_updated,
    registration_expiration,
    insurance_policy_id, loan_record_id,
    status, notes

MaintenanceSchedule:
    vehicle_record_id,
    service_type,
    interval_miles, interval_months,
    last_service_date, last_service_mileage,
    next_due_date, next_due_mileage,
    estimated_cost, provider, notes

ServiceRecord:
    vehicle_record_id,
    date, mileage,
    service_type, provider, cost,
    parts[], notes,
    document_id
```

All three are `structured_records` rows with a JSONB `data` blob; the type-discriminator is `record_type`.

---

## Auto-domain document categories (defined but not surfaced on this page)

From `api/constants.py`:

```
registration, title, insurance_card, service_receipt, recall_notice,
purchase_agreement, lease_agreement, inspection_report, warranty, owners_manual
```

These documents exist (they're in the global Documents list when filtered by `domain=auto`), but the Auto page itself never shows them. No "documents about this vehicle" panel, no recall-notice surfacing, no quick view of the registration card or insurance card.

---

## What's on the page in production right now

Live snapshot (today):

| Section | Count |
|---|---|
| Vehicles | 5 (with apparent duplicates — 3× Sienna, 2× Dakota) |
| Maintenance schedules | 0 |
| Service records | 0 |
| Pending auto actions | 2 |

That `5 vehicles with dupes / 0 schedules / 0 services` distribution is itself a finding: the page has no UI to deduplicate, edit, or hand-create the maintenance scaffolding that would make everything else useful. Vehicles arrive via AI extraction from uploaded registrations/insurance cards; nothing further is built on top.

---

## Concrete gaps to discuss

**Discoverability / navigation**
- No drill-in: vehicle is a flat row, not a clickable card that takes you to a per-vehicle page.
- No way to see which actions/services/schedules belong to which vehicle other than reading the description text.
- No way to view the source registration/insurance/title document from the vehicle row.

**Data the page doesn't display but already exists**
- VIN, purchase price, purchase date, status (sold/totaled).
- Insurance policy + loan/lien linkage (the foreign-key fields are on the schema, never rendered).
- Maintenance schedules, service-record parts/notes, mileage-updated date.
- Mileage time series (`time_series_metrics` table stores every Log event but no chart anywhere — there's no trends integration for mileage).

**Things the system can't do at all from this page**
- Add / edit / delete / merge a vehicle (you'd hit the DB or upload a new doc).
- Add a maintenance schedule manually (so you can't track an oil change interval unless an AI extraction created it).
- Log a service event without it being attached to an uploaded receipt.
- Acknowledge a recall notice.
- See cost-of-ownership rollups (total spend YTD, $/mile).

**Triage**
- Registration-expiration warning is per-vehicle inline and easy to miss in a list.
- No surfacing of recall notices or open insurance claims relevant to a vehicle.

**Cross-section relationships**
- The page treats vehicles, schedules, services, and actions as four separate flat lists. They're actually a graph (vehicle → schedules → actions; vehicle → services). A redesign probably wants to make a vehicle the parent unit.

---

## Suggested questions for the design discussion

1. Should the top level be a vehicle picker (cards or tabs), with all other sections scoped to the selected vehicle?
2. Where should "the documents about this vehicle" live? On the vehicle, or in a separate global Documents view filtered by `domain=auto + linked_vehicle`?
3. Should mileage logging stay inline on the list, or move into a per-vehicle action bar / quick-log shortcut from anywhere?
4. Does a mileage chart belong here, on the Trends page, or both?
5. Is there a "fleet" view someone with 3+ vehicles needs (compare cost/mile, see whose registration expires first), or is a per-vehicle page sufficient?
6. Add/edit/delete UI for vehicles, schedules, services — modal? Drawer? Dedicated form pages?
7. How should the page handle the duplicate-vehicle problem (merging two vehicle records into one without losing service history)?
8. Should recall notices and insurance claims be first-class panels here, or stay in their respective domain pages?

---

## File map (where the code lives)

- Frontend view: `web/dist/index.html` ≈ lines 1660–1753.
- Loader: `web/dist/index.html` `async loadAuto()` ≈ line 3326.
- Mileage handler: `web/dist/index.html` `async logMileage()` ≈ line 3661.
- Mileage endpoint + maintenance recompute: `api/routers/vehicles.py`.
- Record schemas: `api/schemas/auto.py`.
- Generic record CRUD: `api/routers/records.py` (vehicles/schedules/services are all `structured_records`).
- Domain + category list: `api/constants.py`.
