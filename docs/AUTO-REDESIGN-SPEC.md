# LifeOS Auto Page Redesign — Multi-Phase Spec

> Handoff document for Claude Code. Transforms the Auto page from a flat
> read-only inventory into a per-vehicle hub for maintenance, costs, documents,
> recalls, and business mileage reimbursement. Each phase is self-contained
> and ships independently.

---

## Document Purpose

The current Auto page (`web/dist/index.html` ≈ lines 1660–1753) shows three
concatenated flat lists: vehicles, upcoming maintenance, and service history.
Most of the data already captured in `structured_records` and
`time_series_metrics` is never rendered, there is no per-vehicle drill-in,
and there is no UI to create, edit, merge, or delete records. This spec
redesigns the page around a **vehicle-as-parent-unit** model and adds the
operations and surfaces that turn it from inventory into a working tool.

Each phase below has:
- An **overview table** with scope, dependencies, and primary files touched
- **Prerequisites** — what must already exist
- **Scope** — what is and isn't part of the phase
- **Backend changes** — schemas, endpoints, business logic
- **Frontend changes** — routes, views, components
- **Implementation notes** — gotchas, ordering, decisions
- **Testing checklist**

---

## Phase Summary

| # | Phase | Unblocks | Estimated effort |
|---|-------|----------|------------------|
| 1 | Per-Vehicle Drill-In Foundation | Everything else | M |
| 2 | Vehicle CRUD & Merge | Clean data, manual operations | M |
| 3 | Maintenance Schedules | Action-item engine becomes useful | L |
| 4 | Service History & Cost Rollups | $/mile, manual logging | M |
| 5 | Fleet Overview Redesign | At-a-glance triage | M |
| 6 | Documents Panel | Per-vehicle doc surfacing | S |
| 7 | Recall Surfacing & NHTSA Integration | First-class recall handling | M |
| 8 | Mileage Trends & Predictive ETAs | Mileage chart, "due in X weeks" | S |
| 9 | Mileage Reimbursement Reports (CRS) | Business mileage / IRS reports | L |
| 10 | Cross-System Integration | Coach, Tempo, Action item routing | S |

Recommended build order is sequential. Phases 1–3 are the unblocking
foundation. Phases 4–8 are layered value. Phase 9 is the business-side
addition for CRS taxes. Phase 10 wires the page into the rest of the
ecosystem.

---

## Phase 1: Per-Vehicle Drill-In Foundation

| Field | Value |
|-------|-------|
| **Goal** | Make a vehicle a clickable entity with its own page |
| **Scope** | Routing, hero strip, render all existing schema fields |
| **Prerequisites** | None — current Auto page already loads vehicles |
| **Primary files** | `web/dist/index.html`, `api/routers/records.py`, `api/routers/vehicles.py` |
| **Out of scope** | CRUD (Phase 2), merge (Phase 2), schedules UI (Phase 3) |

### Goal

When Dave clicks a vehicle on the Auto page, he lands on
`/auto/{vehicle_id}` — a per-vehicle hub that surfaces every field already
stored on the vehicle record plus skeleton sections for the rest of the
domain. This phase intentionally renders read-only data; subsequent phases
add the CRUD and richer panels.

### Scope

In:
- New SPA route `/auto/{vehicle_id}` (preserve Alpine.js single-page pattern)
- Hero strip with vehicle identity and the "next thing you need to know"
- Render every field on `Vehicle` schema (including VIN, purchase data,
  status, notes — none of which display today)
- Skeleton sections for Maintenance, Service History, Documents,
  Insurance & Loan, Costs (each shows "coming in Phase N" placeholder text)
- Vehicle rows on the existing fleet list become clickable cards

Out:
- Inline editing (Phase 2)
- Schedule UI (Phase 3)
- Document panel content (Phase 6)
- Cost rollups (Phase 4)

### Backend changes

No new endpoints required. Confirm existing endpoints return all schema
fields:

```
GET /api/records/{id}                    → must return full Vehicle data blob
GET /api/records?record_type=vehicle     → must include status field for filtering
```

Add `status` filter support to `GET /api/records` if not already present
(used to hide sold/totaled vehicles from the default fleet view). Soft-archive
behavior is wired in Phase 2; for now just make sure the field round-trips.

### Frontend changes

**Routing.** Use the existing view-state pattern in `index.html`. Add a
`selectedVehicleId` to the Alpine store and a view value `auto-vehicle`.
Browser back/forward should work via `history.pushState` (match whatever
pattern the rest of the app uses — e.g. how Documents drill-in works if it
exists).

**Hero strip.** Top of page:
- Line 1: `{year} {make} {model}` + optional `{trim}` smaller
- Line 2: license plate · color · status badge (active / sold / totaled)
- Right side: current mileage with thousands separator + "Updated {ago}"
- "Next up" callout — the single highest-priority item, computed as:
  1. Registration expiring within 30 days → red
  2. Overdue maintenance → red
  3. Open recall → red (Phase 7 fills this in; show placeholder)
  4. Maintenance due within 500 mi or 30 days → yellow
  5. Otherwise → green "All clear"

**Overview section.**

A two-column key/value grid showing:
- VIN
- Purchase date · purchase price
- Mileage-updated date (from `mileage_updated`)
- Registration expiration (highlight if within 60 days)
- Notes (free text)
- Linked insurance policy (if `insurance_policy_id` set — show name, link to insurance domain)
- Linked loan record (if `loan_record_id` set — show name, link to loan domain)

**Skeleton sections.** Maintenance, Service History, Documents, Costs each
get a heading and a placeholder card with a one-line description of what
will live there in later phases.

**Mileage logging.** Move the existing inline mileage logger from the fleet
list into the vehicle hero strip. Keep the fleet-list version too for now —
Phase 5 will redesign the fleet view.

### Implementation notes

- The current fleet list does not have a visual affordance for "click me."
  Add cursor:pointer and a hover state so the rows read as interactive.
- The `mileage_updated` timestamp needs a "X days ago" formatter. There's
  likely one already used elsewhere (registration_expiration formatting).
- Preserve the `badge-priority-high` styling for the registration-expiring
  warning — repurpose it for any "next up" item that's red.
- The route `/auto/{vehicle_id}` should 404 (or show a friendly empty state)
  when the ID doesn't exist or the vehicle is soft-deleted.

### Testing checklist

- [ ] Clicking a vehicle on the Auto page navigates to `/auto/{id}`
- [ ] Browser back returns to the fleet list with state intact
- [ ] All `Vehicle` schema fields are visible on the page (use a vehicle
      with VIN, purchase data, and notes populated)
- [ ] Vehicle with no purchase date / no VIN renders without errors
- [ ] "Next up" surfaces registration expiry correctly when within 30 days
- [ ] "Next up" shows green "All clear" when nothing urgent
- [ ] Mileage logging from the vehicle page works and updates the
      `current_mileage` and `mileage_updated` timestamps
- [ ] Invalid `/auto/{bad_id}` URL shows an empty/not-found state
- [ ] Linked insurance and loan records appear with working links when FKs
      are set

---

## Phase 2: Vehicle CRUD & Merge

| Field | Value |
|-------|-------|
| **Goal** | Add/edit/archive vehicles; merge duplicates without losing history |
| **Scope** | Vehicle CRUD endpoints + UI, merge endpoint + UI |
| **Prerequisites** | Phase 1 |
| **Primary files** | `api/routers/vehicles.py`, `api/routers/records.py`, `web/dist/index.html` |
| **Out of scope** | Schedule CRUD (Phase 3), service CRUD (Phase 4) |

### Goal

The live data state (5 vehicles with 3× Sienna and 2× Dakota duplicates,
zero schedules) makes this the second-most-critical phase. Without merge,
the cleanup must happen by hand in psql. Without manual add/edit, vehicle
records can only enter the system via AI document extraction, which limits
the tool's usefulness for anything that doesn't have an associated upload.

### Scope

In:
- `POST /api/vehicles` — create a vehicle record (manual add)
- `PATCH /api/vehicles/{id}` — partial update of any field
- `POST /api/vehicles/{id}/archive` — soft delete (status → archived)
- `POST /api/vehicles/merge` — merge source into target, reassign FKs
- UI: "Add vehicle" button on Auto page
- UI: Edit drawer/modal on vehicle page (inline edits acceptable for
  single fields)
- UI: "Merge with…" action on vehicle page with confirmation diff
- UI: Status lifecycle (active → sold / totaled / archived)

Out:
- Hard delete (intentional — preserves history)
- Bulk operations

### Backend changes

**New endpoint: `POST /api/vehicles`**

Body: any subset of `Vehicle` fields. `year`, `make`, `model` are required.
Returns the new record.

**New endpoint: `PATCH /api/vehicles/{id}`**

Body: any subset of `Vehicle` fields. Returns the updated record. Should
not allow changing `vehicle_record_id` references from outside (those move
via merge).

**New endpoint: `POST /api/vehicles/{id}/archive`**

Sets `status = 'archived'`. Does not delete. Returns the updated record.
Archived vehicles are excluded from default fleet queries but included
when `?include_archived=true`.

**New endpoint: `POST /api/vehicles/merge`**

Body:
```json
{
  "source_vehicle_id": "...",
  "target_vehicle_id": "...",
  "field_resolutions": {
    "vin": "target",          // or "source"
    "purchase_price": "source",
    "notes": "concat"         // special: append source notes to target
  }
}
```

Behavior:
1. Reassign all `service_record.vehicle_record_id` from source → target
2. Reassign all `maintenance_schedule.vehicle_record_id` from source → target
3. Reassign all `action_items` referencing the source vehicle → target
4. Reassign all `time_series_metrics` rows tagged to source → target
5. Reassign any documents linked to source vehicle → target
6. Apply field resolutions to target (defaulting to target's value for any
   field not listed)
7. Soft-delete source (status → merged, with `merged_into_vehicle_id` set)
8. Wrap everything in a single DB transaction

Add `merged_into_vehicle_id` column to vehicle records data blob so the
audit trail is preserved.

### Frontend changes

**Auto page header.** "Add vehicle" button next to existing fleet list.
Opens a drawer with a form for the core fields (year, make, model, plate,
VIN, color, current mileage). Other fields editable from the vehicle page
after creation.

**Vehicle page edit affordances.**

Two options — pick one and apply consistently:
- **Inline edit:** click any field, it becomes an input, save on blur/enter
- **Edit drawer:** "Edit details" button opens a side drawer with all fields

Inline is faster but harder to validate. Drawer is more conventional. Match
whatever pattern is used elsewhere in LifeOS for record editing.

**Status changes.** Status badge on the hero strip is clickable. Opens a
small menu: Active / Sold / Totaled / Archived. Selecting Sold or Totaled
prompts for a date (stored in notes or a new `disposed_date` field). Archived
vehicles still accessible via `/auto/{id}` but excluded from fleet view by
default.

**Merge flow.**

From the vehicle page, "Actions" menu has "Merge with…":
1. Modal opens with a vehicle picker (excludes self and archived)
2. After selection, show a **side-by-side diff** of every field
3. For each conflicting field, radio button: keep source / keep target / concat (notes only)
4. Show a summary of what will move: "12 service records, 4 schedules,
   3 action items, 2 documents"
5. "Merge" button requires typing the target vehicle's plate to confirm
6. On success, redirect to the target vehicle's page

### Implementation notes

- Status values: `active`, `sold`, `totaled`, `archived`, `merged`. The
  `merged` status is special — these records exist only as audit trail and
  should never appear in any UI list.
- The merge confirmation step (typing the plate) is a guard against
  catastrophic mistakes — service history is the highest-value data in this
  system and merging the wrong direction is hard to undo.
- The merge endpoint must be idempotent on retry — if a partial failure
  leaves things in a weird state, re-running should converge to the correct
  end state. The DB transaction wrapper handles this.
- VIN-based duplicate detection would be a nice future addition: when adding
  a vehicle, if the VIN matches an existing one, suggest merge instead.

### Testing checklist

- [ ] Manual vehicle add works with only required fields
- [ ] Manual vehicle add works with all fields populated
- [ ] PATCH allows clearing a field (set to null)
- [ ] Archive removes vehicle from default fleet list
- [ ] Archived vehicle still accessible via direct URL
- [ ] Merge preview correctly shows count of service records, schedules,
      action items, and documents that will move
- [ ] Merge with field resolutions correctly applies target's chosen values
- [ ] Merge of two of Dave's actual duplicate Siennas preserves all history
- [ ] Source vehicle becomes inaccessible from fleet view after merge
- [ ] Source vehicle URL redirects to target after merge
- [ ] Cannot merge a vehicle into itself
- [ ] Cannot merge archived or merged vehicles
- [ ] Plate-confirmation typo prevents merge
- [ ] All operations leave the action-item engine in a consistent state
      (re-run recompute should be a no-op afterwards)

---

## Phase 3: Maintenance Schedules

| Field | Value |
|-------|-------|
| **Goal** | UI + presets to make the action-item engine actually fire |
| **Scope** | Schedule template library, schedule CRUD, timeline view, predictive math |
| **Prerequisites** | Phases 1–2 |
| **Primary files** | `api/routers/records.py`, `api/routers/vehicles.py`, new `api/data/maintenance_templates.py`, `web/dist/index.html` |
| **Out of scope** | Service record manual entry (Phase 4), mileage chart UI (Phase 8) |

### Goal

The current state has zero maintenance schedules, which is why the action
item engine has nothing to fire on. The schema for `MaintenanceSchedule`
already exists; what's missing is a UI to create them and a preset library
so creating typical schedules is one click rather than ten forms.

### Scope

In:
- Maintenance template library (presets by category, not strictly per
  make/model — see notes)
- `POST /api/vehicles/{id}/schedules/apply-template` — apply a named
  template, creates multiple schedule records in one call
- `POST /api/maintenance-schedules` — manual create
- `PATCH /api/maintenance-schedules/{id}` — edit
- `DELETE /api/maintenance-schedules/{id}` — delete (hard delete OK,
  these are easy to recreate)
- UI: Schedules section on vehicle page
- UI: "Add typical schedules" template picker
- UI: Manual "Add schedule" form
- UI: Timeline view — schedule entries plotted against mileage and dates
- Predictive next-due math layered onto the existing action-item creation

Out:
- Per-make/model templates with model-year specifics (Phase X future — see
  notes; the generic templates handle 90% of cases)
- Service record creation from a schedule (Phase 4)

### Backend changes

**Template library.**

Create `api/data/maintenance_templates.py` with named template sets:

```python
TEMPLATES = {
  "standard_gas_passenger": {
    "label": "Standard gas car",
    "description": "Typical schedule for most gas passenger vehicles",
    "schedules": [
      {"service_type": "Oil change", "interval_miles": 5000, "interval_months": 6, "estimated_cost": 75},
      {"service_type": "Tire rotation", "interval_miles": 7500, "interval_months": 6, "estimated_cost": 30},
      {"service_type": "Air filter", "interval_miles": 20000, "interval_months": 24, "estimated_cost": 35},
      {"service_type": "Cabin air filter", "interval_miles": 20000, "interval_months": 24, "estimated_cost": 40},
      {"service_type": "Brake inspection", "interval_miles": 15000, "interval_months": 12, "estimated_cost": 0},
      {"service_type": "Brake fluid flush", "interval_miles": 30000, "interval_months": 36, "estimated_cost": 100},
      {"service_type": "Coolant flush", "interval_miles": 60000, "interval_months": 60, "estimated_cost": 150},
      {"service_type": "Transmission fluid", "interval_miles": 60000, "interval_months": 60, "estimated_cost": 200},
      {"service_type": "Spark plugs", "interval_miles": 60000, "interval_months": 60, "estimated_cost": 250},
      {"service_type": "Battery check", "interval_miles": None, "interval_months": 12, "estimated_cost": 0},
      {"service_type": "Registration renewal", "interval_miles": None, "interval_months": 12, "estimated_cost": None}
    ]
  },
  "high_mileage_truck": {
    "label": "Truck / heavy-use",
    "description": "Shorter oil change interval, more frequent fluids",
    "schedules": [ ... ]
  },
  "hybrid": { ... },
  "ev": { ... },
  "minimal": {
    "label": "Minimal (oil + tires + registration)",
    "description": "Just the basics",
    "schedules": [ ... ]
  }
}
```

These don't need to be perfect — the template is a starting point. Each
applied schedule should be editable afterward.

**Endpoint: `GET /api/maintenance-templates`**

Returns the templates dict (label, description, schedule count). Frontend
uses this to render the picker.

**Endpoint: `POST /api/vehicles/{id}/schedules/apply-template`**

Body:
```json
{
  "template_key": "standard_gas_passenger",
  "skip_duplicates": true
}
```

Creates schedule records for each entry in the template. If
`skip_duplicates` is true (default), skip any whose `service_type` already
exists for this vehicle. Returns the list of created schedule IDs.

After creation, run the same recompute logic that `POST /api/vehicles/{id}/mileage`
uses so action items get created for anything imminently due.

**Endpoint: `POST /api/maintenance-schedules`**

Body: full `MaintenanceSchedule` fields including `vehicle_record_id`.
Standard record creation.

**Endpoint: `PATCH /api/maintenance-schedules/{id}`**

Partial update.

**Endpoint: `DELETE /api/maintenance-schedules/{id}`**

Hard delete the schedule. Does NOT delete its associated action items — but
should mark them with `schedule_deleted=true` in metadata so the UI can
distinguish abandoned-on-purpose vs forgotten.

**Predictive math.**

When recomputing schedules after a mileage log, also compute and store:
- `predicted_due_date` — based on miles-per-day cadence over the last 90
  days (or shorter if vehicle is newer), project when `current_mileage` will
  hit `next_due_mileage`. Store on the schedule record.
- This drives the "Due in 6 weeks" copy on the vehicle page.

Miles-per-day calc:
```
recent_metrics = SELECT * FROM time_series_metrics
                 WHERE entity_id = vehicle_id AND metric_type = 'mileage'
                 AND recorded_at >= now() - interval '90 days'
                 ORDER BY recorded_at
if len(recent_metrics) < 2: return None    # not enough data
miles = last.value - first.value
days = (last.recorded_at - first.recorded_at).days
if days < 7 or miles <= 0: return None    # too noisy
mpd = miles / days
```

### Frontend changes

**Schedules section on vehicle page.**

List of schedule rows, sorted by "soonest due" (whichever of date or mileage
projection is closer). Each row:
- Service type
- Interval (e.g. "Every 5,000 mi or 6 months")
- Last service: date + mileage (if logged)
- Next due: date + mileage + predicted ETA ("≈ Mar 15" if computed)
- Estimated cost (if set)
- Edit / Delete buttons

Empty state when no schedules: prominent card with two buttons:
- "Add typical schedules" → template picker
- "Add a single schedule" → manual form

**Template picker.**

Modal/drawer:
- List of templates with label + description + schedule count
- Selecting one shows the full list of schedules it'll create with checkboxes
- All checked by default; user can uncheck items they don't want
- "Apply" button creates everything; redirects back to vehicle page with
  the schedules section populated

**Manual schedule form.**

Drawer with fields: service type (free text or dropdown of common types),
interval_miles, interval_months, last_service_date, last_service_mileage,
estimated_cost, provider, notes.

At least one of `interval_miles` or `interval_months` must be set.

**Timeline view.**

A small horizontal strip at the top of the schedules section showing the
next 6 months. Markers show:
- Date-based schedules at their `next_due_date`
- Mileage-based schedules at their `predicted_due_date` (with a hatched
  marker style to indicate it's a projection, not a hard date)
- Today is a vertical line
- Hover/tap shows which schedule and the details

Keep this lightweight — Chart.js or a simple inline SVG, not D3.

### Implementation notes

- Templates are deliberately generic, not make/model-specific. Adding a
  Toyota-specific template that knows about timing belt replacement at
  90k for older models is a future enhancement — for now, Dave can edit
  the standard template's output to add vehicle-specific items.
- The `skip_duplicates` logic should match on `service_type` exactly. If
  Dave wants two separate "Oil change" schedules (synthetic vs conventional
  for two different drivers' habits, say), he can manually add the second.
- Registration renewal is intentionally included in the standard template
  as a date-only schedule. This lets the action-item engine produce
  "Registration due in 30 days" alerts automatically once a renewal is
  logged.
- The mpd calculation is sensitive to short time windows. Require at least
  7 days of data. Otherwise show "Not enough mileage data to predict" on
  the schedule row.

### Testing checklist

- [ ] Template picker lists all templates with correct schedule counts
- [ ] Applying "standard_gas_passenger" to a vehicle with zero schedules
      creates all expected entries
- [ ] Reapplying the same template with skip_duplicates skips everything
- [ ] Reapplying with skip_duplicates=false creates duplicates (verify
      this is what's expected)
- [ ] Manual add with only `interval_months` set works (no mileage interval)
- [ ] Manual add with only `interval_miles` set works (no date interval)
- [ ] Manual add with neither interval fails validation
- [ ] Editing a schedule recomputes the next-due fields
- [ ] Deleting a schedule preserves prior action items but tags them
- [ ] Predictive ETA appears when there's enough mileage history
- [ ] Predictive ETA absent when <7 days of data
- [ ] Action items fire correctly after applying a template (logged in the
      pending actions section)
- [ ] Timeline view renders correctly with mixed date and mileage schedules
- [ ] Timeline marks projected entries with distinct visual style

---

## Phase 4: Service History & Cost Rollups

| Field | Value |
|-------|-------|
| **Goal** | Manual service logging, per-vehicle history, cost analytics |
| **Scope** | Service record CRUD UI, categories, cost rollups |
| **Prerequisites** | Phases 1–3 |
| **Primary files** | `api/routers/records.py`, `web/dist/index.html` |
| **Out of scope** | Service-receipt auto-extraction (existing pipeline, separate concern) |

### Goal

Service records currently arrive only via AI document extraction. Dave can't
log "changed my own oil" or "paid cash at Joe's Garage." Cost rollups don't
exist anywhere even though all the source data is sitting in the records
table.

### Scope

In:
- `POST /api/service-records` — manual create
- `PATCH /api/service-records/{id}` — edit
- `DELETE /api/service-records/{id}` — delete (with confirm)
- Service category field — add to `ServiceRecord` schema if not present:
  one of `preventive`, `repair`, `tires`, `bodywork`, `registration`,
  `inspection`, `other`
- UI: Service history section on vehicle page (replaces the global flat
  list for per-vehicle context)
- UI: "Log a service" form on vehicle page
- UI: Cost rollups panel — total spend, $/mile, YTD, by category
- Optional link: when logging service for a known schedule, mark the
  schedule's `last_service_date` and `last_service_mileage`

Out:
- Receipt scanning (existing pipeline)
- Multi-vehicle cost comparisons (Phase 5 fleet view handles this)

### Backend changes

**Schema addition.**

Add `category` field to `ServiceRecord` schema. Enum: `preventive`,
`repair`, `tires`, `bodywork`, `registration`, `inspection`, `other`.
Backfill existing records to `other` on migration.

**Endpoint: `POST /api/service-records`**

Body: full `ServiceRecord` fields. `vehicle_record_id`, `date`, and
`service_type` are required. Returns created record.

Optional `link_to_schedule_id` query param: if provided, updates that
schedule's `last_service_date` and `last_service_mileage` to match the new
record, and recomputes `next_due_date` / `next_due_mileage`.

**Endpoint: `PATCH /api/service-records/{id}`**

Partial update.

**Endpoint: `DELETE /api/service-records/{id}`**

Hard delete OK — these can be re-entered.

**Endpoint: `GET /api/vehicles/{id}/cost-summary`**

Returns:
```json
{
  "lifetime_total": 4823.50,
  "lifetime_per_mile": 0.18,
  "ytd_total": 1240.00,
  "by_category": {
    "preventive": 580.00,
    "repair": 660.00,
    ...
  },
  "by_year": {
    "2024": 1840.00,
    "2025": 2740.00,
    "2026": 243.50
  }
}
```

Per-mile calc: `lifetime_total / (current_mileage - earliest_known_mileage)`
where earliest_known_mileage is the lowest mileage in service records or
mileage logs. If <1000 miles of history, return null for per_mile.

### Frontend changes

**Service history section on vehicle page.**

Chronological table (newest first) with:
- Date
- Mileage
- Service type · category badge
- Provider
- Cost
- Doc link (if `source_document_id`)
- Expand/edit/delete actions

Show parts and notes when row is expanded.

**Log a service form.**

Drawer with fields: date (default today), mileage (default current_mileage),
service type (free text), category (dropdown), provider, cost, parts
(comma-separated), notes.

If there's a maintenance schedule with matching service_type, show: "This
looks like the [Oil change] you had scheduled — link it?" with a checkbox.
Checked by default. If checked, send the `link_to_schedule_id` param.

**Cost rollups panel.**

Top of the service history section, or as a separate "Costs" tab/section:
- Big number: Total lifetime spend
- $/mile (or "Not enough data" if < 1000 mi history)
- YTD spend
- Small bar chart: spend by category (horizontal bars with category labels)
- Small line chart: spend by year (last 5 years)

### Implementation notes

- The `link_to_schedule_id` feature is the seam between this phase and
  Phase 3 — it's the user flow that turns "I had this oil change" into
  "schedule's next-due is now correctly forward-looking." Worth making
  this prompt nice.
- Cost per mile is sensitive to incomplete data. The "Not enough data"
  guardrail (<1000 miles of history) keeps absurd numbers off the page.
- Category defaults to `preventive` for new entries (most common case).
- Parts as a comma-separated string on input that converts to/from
  `list[str]` on the schema is fine — keep the UI simple.

### Testing checklist

- [ ] Manual service log with required fields creates a record
- [ ] Service log with full fields (parts, notes, cost) round-trips
- [ ] Linking a service log to a schedule updates the schedule's last_*
      fields and recomputes next-due
- [ ] Cost summary returns correct lifetime total for a vehicle with
      multiple services
- [ ] Cost-per-mile returns null when history is too short
- [ ] Cost-per-mile computes correctly when history exceeds 1000 miles
- [ ] YTD resets at year boundary
- [ ] Category breakdown sums to lifetime total
- [ ] Editing a service record adjusts the cost summary
- [ ] Deleting a service record adjusts the cost summary
- [ ] Document link works for records with `source_document_id` set

---

## Phase 5: Fleet Overview Redesign

| Field | Value |
|-------|-------|
| **Goal** | Replace the flat list with a card grid that surfaces what matters at a glance |
| **Scope** | Card layout, status surfacing, aggregate counters, quick-log FAB |
| **Prerequisites** | Phases 1–4 (cards need rich data to be worth surfacing) |
| **Primary files** | `web/dist/index.html` |
| **Out of scope** | Mileage chart on cards (Phase 8 adds sparklines) |

### Goal

Once vehicles have rich data (schedules, services, costs), the current flat
list is too dense and doesn't surface urgency. Replace it with cards that
read as glanceable status, plus aggregate fleet stats.

### Scope

In:
- Card grid layout for vehicles (replaces table-style rows)
- Status surfacing on each card (the same "next up" logic from Phase 1's
  hero strip, just smaller)
- Color-coded left edge or top bar for urgency level
- Aggregate counters at top: total fleet mileage, YTD spend, open recalls
  count (Phase 7 populates this), registrations expiring in 60 days
- Floating "Log mileage" action button with a vehicle picker (logs from
  anywhere on the Auto page)
- Filter: show/hide archived vehicles

Out:
- Mileage sparkline on cards (Phase 8)
- Comparative fleet analytics view (future)

### Backend changes

**Endpoint: `GET /api/auto/fleet-summary`**

Returns:
```json
{
  "vehicle_count": 3,
  "total_fleet_mileage": 145230,
  "ytd_spend": 4280.00,
  "open_recalls": 1,
  "registrations_expiring_60d": 1,
  "overdue_maintenance": 2
}
```

Single endpoint to avoid multiple round trips when rendering the header.

Could also be inlined into the existing fleet load — pick whichever is
cleaner.

### Frontend changes

**Card design.**

Each card:
- Left edge: 4px color bar — red (urgent) / yellow (soon) / green (clear)
- Top row: year/make/model (larger), trim (smaller)
- Mid row: current mileage, plate
- Status line: the single highest-priority "next up" item
- Right edge: small chevron indicating clickable

Card click → vehicle drill-in page.

**Aggregate header.**

Four counters across the top:
- Total fleet mileage (sum of `current_mileage`)
- YTD spend (sum of YTD spend across vehicles)
- Open recalls (count from Phase 7)
- Registrations expiring (count within 60 days)

Each counter is itself clickable where it makes sense (e.g. clicking
"Open recalls" filters fleet to vehicles with open recalls).

**Floating action button.**

Bottom-right of the Auto page. Tap → modal with:
- Vehicle picker (dropdown of active vehicles)
- Mileage input
- Log button

Same backend endpoint as inline log. Closes on success with a brief toast
("Logged 47,820 mi for Sienna").

**Archived filter.**

Small toggle: "Show archived" — when on, includes archived vehicles in the
grid (with a visually muted style).

### Implementation notes

- The status-line priority order from Phase 1 should be reused here.
  Extract that logic to a shared helper so it stays consistent between
  card view and hero strip.
- The aggregate counters should refresh on any mileage log or service
  entry. Easiest is a re-fetch after any mutation.
- Mobile considerations: cards should stack to single column below ~640px.
  The current Auto page doesn't appear to be mobile-optimized, but new work
  should be (Phase 11 is the mobile PWA odometer capture and this'll be
  the first place mobile users land).

### Testing checklist

- [ ] Fleet view renders as cards instead of rows
- [ ] Card click navigates to vehicle drill-in
- [ ] Color bar correctly reflects urgency state
- [ ] Status line shows the highest-priority "next up" item
- [ ] Aggregate counters compute correctly across vehicles
- [ ] FAB log mileage works from any scroll position on the page
- [ ] Toast confirms successful log
- [ ] Archived toggle hides/shows archived vehicles
- [ ] Empty state when no vehicles still works
- [ ] Cards stack to single column on narrow viewport

---

## Phase 6: Documents Panel

| Field | Value |
|-------|-------|
| **Goal** | Surface vehicle-related documents on the vehicle page |
| **Scope** | Per-vehicle document filtering, thumbnail grid, expiration callouts |
| **Prerequisites** | Phases 1–2 |
| **Primary files** | `api/routers/records.py` or whatever serves documents, `web/dist/index.html` |
| **Out of scope** | Document upload (existing flow), AI extraction (existing pipeline) |

### Goal

Registration, title, insurance card, recall notices, and service receipts
all exist as documents in the system but never appear on the vehicle page.
Surface them with appropriate context (expiration dates, document type) and
make it easy to view the source.

### Scope

In:
- Backend support for filtering documents by `linked_vehicle_id` (or
  whatever the FK pattern is — check existing code)
- Documents panel on vehicle drill-in page
- Thumbnail grid grouped by document category
- Expiration callouts for documents that have them (registration, insurance
  card)
- Click-through to existing document detail view

Out:
- Re-linking documents to vehicles (manual association if a doc is wrongly
  linked — defer to a doc-management phase)
- New document categories

### Backend changes

Confirm document records have a `linked_vehicle_id` (or equivalent — may be
inside a `links` JSONB blob). If not, add it; AI extraction should already
be populating this when registration/insurance docs are processed.

**Endpoint: `GET /api/vehicles/{id}/documents`**

Returns documents grouped by category:
```json
{
  "registration": [ { "id": "...", "uploaded_at": "...", "thumbnail_url": "...", "expiration_date": "..." } ],
  "insurance_card": [ ... ],
  "title": [ ... ],
  "service_receipt": [ ... ],
  "recall_notice": [ ... ],
  ...
}
```

Most-recent-first within each category.

### Frontend changes

**Documents section on vehicle page.**

Header: "Documents" with total count.

For each category that has at least one document:
- Subheading with category name and count
- Thumbnail grid (3-4 per row)
- Each thumbnail shows: doc preview, uploaded date, expiration date (if
  applicable, with red badge if expired/expiring)
- Click thumbnail → existing document detail view

Empty state if no documents: "No documents linked to this vehicle yet.
Upload one and link it here."

**Expiration handling.**

Registration and insurance card categories should display expiration
prominently. The Phase 1 "Next up" calculation should also look here for
expiring insurance, not just `registration_expiration` on the vehicle
record.

### Implementation notes

- The existing document upload flow already handles category assignment
  and AI extraction. This phase is purely a surfacing improvement.
- If a doc is linked to a since-merged vehicle, it should follow the merge
  (Phase 2 already handles this).
- The thumbnail rendering should reuse whatever component the global
  Documents view uses — don't reinvent.

### Testing checklist

- [ ] Documents section appears on vehicle page when documents are linked
- [ ] Categories are grouped correctly
- [ ] Most-recent-first ordering within each category
- [ ] Expiration date displays on registration and insurance cards
- [ ] Expired/expiring documents show red badge
- [ ] Click-through opens existing document detail view
- [ ] Empty state appears for vehicles with no linked documents
- [ ] After a merge, documents follow the target vehicle

---

## Phase 7: Recall Surfacing & NHTSA Integration

| Field | Value |
|-------|-------|
| **Goal** | First-class recall handling with automatic NHTSA lookups |
| **Scope** | NHTSA API integration, recall records, vehicle page panel, acknowledge flow |
| **Prerequisites** | Phases 1–2 (need vehicle with VIN) |
| **Primary files** | New `api/routers/recalls.py`, new `api/services/nhtsa.py`, `web/dist/index.html` |
| **Out of scope** | International recall databases (NHTSA is US-only) |

### Goal

NHTSA exposes a free, no-auth recall API. Background-check each vehicle's
VIN against it and surface results as first-class items, separate from the
existing "recall_notice" document category. Many recalls never produce a
physical letter — automatic VIN-based lookup catches the ones that would
otherwise be missed entirely.

### Scope

In:
- Background job: weekly NHTSA recall check by VIN for all active vehicles
- New `vehicle_recall` structured_records type
- Acknowledge / mark-resolved flow
- Vehicle page panel: "Open recalls" with severity
- Aggregate count for fleet header (Phase 5)
- Action item creation for new recalls

Out:
- Real-time push notifications (action items + Coach are sufficient)
- Manual recall entry (if NHTSA doesn't have it, the user can attach a
  recall_notice document the old way)

### Backend changes

**NHTSA API.**

Endpoint: `https://api.nhtsa.gov/recalls/recallsByVehicle?make={make}&model={model}&modelYear={year}`

Also: `https://api.nhtsa.gov/recalls/getRecallsByVIN?vin={vin}` — preferred
because it's vehicle-specific.

Both return JSON with `Results: []` containing `NHTSACampaignNumber`,
`Component`, `Summary`, `Consequence`, `Remedy`, `ReportReceivedDate`.

**Schema addition.**

```python
VehicleRecall:
    vehicle_record_id: str
    nhtsa_campaign_number: str  # unique identifier
    component: str              # "STEERING:LINKAGES"
    summary: str                # short description
    consequence: str            # safety impact
    remedy: str                 # how it's fixed
    report_received_date: date  # when NHTSA logged it
    status: str                 # "open" | "acknowledged" | "resolved"
    resolved_service_record_id: Optional[str]  # link to the service that fixed it
    discovered_at: datetime     # when our system first saw it
    acknowledged_at: Optional[datetime]
    resolved_at: Optional[datetime]
    notes: Optional[str]
```

Stored as `record_type='vehicle_recall'` in `structured_records`.

**Background job.**

`api/services/nhtsa.py`:

```python
def check_recalls_for_vehicle(vehicle_id):
    vehicle = get_vehicle(vehicle_id)
    if not vehicle.vin: return  # need VIN for VIN-based lookup
    response = call_nhtsa_vin_api(vehicle.vin)
    for recall in response['Results']:
        existing = find_recall_by_campaign(vehicle_id, recall['NHTSACampaignNumber'])
        if not existing:
            create_recall_record(vehicle_id, recall)
            create_action_item(...)
```

Schedule: cron or background task, weekly. Also run on-demand when a VIN
is first added to a vehicle.

**Endpoints.**

- `GET /api/vehicles/{id}/recalls?status=open` — list recalls for vehicle
- `POST /api/recalls/{id}/acknowledge` — mark acknowledged
- `POST /api/recalls/{id}/resolve` — body: `{service_record_id?: str, notes?: str}`
- `POST /api/vehicles/{id}/recalls/refresh` — manually trigger NHTSA check

### Frontend changes

**Recalls panel on vehicle page.**

Above the maintenance section if there are open recalls. Each row:
- Severity-style red border
- Component name
- Short summary
- "View details" expander shows full consequence + remedy text
- Actions: "Acknowledge" (I've seen this) and "Mark resolved" (it's fixed)

Resolved recalls collapse into "Resolved (3)" link that expands a list.

**Fleet header.**

Phase 5's "Open recalls" counter pulls from this data.

**Manual refresh.**

Small "Check for recalls" button in the recalls panel. Calls
`POST /api/vehicles/{id}/recalls/refresh` and reloads.

### Implementation notes

- NHTSA's API is free but rate-limited and occasionally slow. Don't block
  vehicle page loads on it — the background job handles freshness, and
  the manual refresh button is for impatient cases.
- The VIN-based endpoint is preferred over make/model/year because it's
  more accurate (some recalls only affect specific production date ranges).
- "Resolve" with a linked service record is the happy path. Allow resolve
  without one (with a notes field) for cases where the work was done
  before LifeOS existed.
- A recall that already exists in the system as a `recall_notice` document
  should not be auto-duplicated. Best-effort matching by NHTSA campaign
  number; otherwise treat as separate (better duplicates than missed).

### Testing checklist

- [ ] NHTSA API integration returns parseable results for a known VIN
- [ ] Background job creates `vehicle_recall` records on first run
- [ ] Re-running background job doesn't duplicate existing recalls
- [ ] Open recalls show in vehicle page panel with severity styling
- [ ] Acknowledge moves recall to acknowledged state
- [ ] Resolve with service record link sets `resolved_service_record_id`
- [ ] Manual refresh button triggers NHTSA check
- [ ] Fleet header open-recalls counter is correct
- [ ] Vehicle without VIN doesn't break (skip silently)
- [ ] Action item is created for new recalls

---

## Phase 8: Mileage Trends & Predictive ETAs

| Field | Value |
|-------|-------|
| **Goal** | Mileage chart on vehicle page + sparkline on fleet cards |
| **Scope** | Trends panel, sparklines, predictive ETA labels |
| **Prerequisites** | Phases 1, 3, 5 |
| **Primary files** | `api/routers/vehicles.py`, `web/dist/index.html` |
| **Out of scope** | Mobile odometer capture (separate concern; defer) |

### Goal

Every mileage log writes to `time_series_metrics` and nothing reads it back.
Surface the chart, both for the vehicle page (full chart) and the fleet
view (sparkline). Use the same data for "due in 6 weeks" ETAs that Phase 3
sets up the math for.

### Scope

In:
- `GET /api/vehicles/{id}/mileage-history` endpoint
- Chart on vehicle page (Chart.js or similar — match what LifeOS uses
  elsewhere)
- Sparklines on fleet cards
- "Miles per month" stat on vehicle page
- Predictive ETA labels everywhere relevant (schedules, "next up" text)

Out:
- Multi-vehicle comparison charts (future fleet-analytics view)
- Mobile photo OCR for odometer (separate phase, optional)

### Backend changes

**Endpoint: `GET /api/vehicles/{id}/mileage-history`**

Query params: `since` (date), `granularity` (`day` | `week` | `month`).

Returns:
```json
{
  "points": [
    {"date": "2024-01-15", "mileage": 38420},
    {"date": "2024-02-10", "mileage": 39100},
    ...
  ],
  "miles_per_day_recent": 18.4,
  "miles_per_month_recent": 559,
  "data_quality": "good" | "limited" | "insufficient"
}
```

The recent-cadence calc reuses Phase 3's mpd logic.

### Frontend changes

**Vehicle page chart.**

A new "Trends" or "Mileage" section below Service History. Line chart
showing mileage over time, with:
- Y axis: mileage
- X axis: date
- Dots at each logged event
- Optional: overlay schedule next-due markers as horizontal reference lines
  ("Oil change at 50k")

Stat block above the chart:
- Miles per month (last 90 days average)
- Total miles tracked in LifeOS
- First/last log dates

**Fleet card sparklines.**

Small inline sparkline (no axes, no labels) showing the last 6 months of
mileage. Subtle visual — purpose is to convey direction and rough rate at
a glance, not exact values.

**Predictive ETA labels.**

Wherever "Next due: 45,000 mi" appears, append "(≈ Mar 15)" when the
predictive date is available. This is the visible payoff of Phase 3's
backend work.

### Implementation notes

- Chart library: whatever LifeOS already uses on the Trends page. Don't
  introduce a new dependency unless necessary.
- Sparklines can be inline SVG (cheap, no library needed). Keep them
  under 100px wide.
- `data_quality` field on the history endpoint flags when ETAs should not
  be shown (insufficient data, sudden gaps, etc.). The frontend can
  display "Not enough data yet" instead of a misleading projection.

### Testing checklist

- [ ] Mileage history endpoint returns correct ordered points
- [ ] Chart renders correctly with sparse data
- [ ] Chart renders correctly with dense data (daily logs)
- [ ] Sparkline renders on fleet cards
- [ ] Predictive ETA appears next to next-due mileage when available
- [ ] No predictive ETA shown when data is insufficient
- [ ] Miles-per-month stat is accurate
- [ ] First/last log dates correct

---

## Phase 9: Mileage Reimbursement Reports (CRS Business)

| Field | Value |
|-------|-------|
| **Goal** | Track business mileage for CRS and generate IRS-compliant reimbursement reports |
| **Scope** | Trip log model, manual entry, IRS rate table, report generation, PDF export |
| **Prerequisites** | Phases 1–2 |
| **Primary files** | New `api/routers/trips.py`, new `api/schemas/trips.py`, new `api/data/irs_mileage_rates.py`, `web/dist/index.html` |
| **Out of scope** | GPS-based automatic trip detection (future), QBO expense sync (Phase 10) |

### Goal

Castle Rock Sky business mileage is a real tax deduction Dave should be
capturing systematically. Build a trip log per vehicle and generate
quarterly/annual reports formatted for IRS-compliant deduction or for
reimbursement bookkeeping into QBO. Personal trips do not get logged here;
this is business-only.

### Scope

In:
- Trip log structured record type
- Per-vehicle trip log UI
- Manual trip entry (start date, end date if multi-day, miles, purpose,
  client, locations)
- IRS standard mileage rate by year (configurable, with documented current
  rates)
- Report generation: by date range, per vehicle and aggregate
- PDF export of reports
- Tax year filtering
- "Tag a service receipt as business expense" path (existing service records
  can be marked deductible — separate from trip mileage)

Out:
- QBO expense sync — Phase 10
- GPS / automatic trip capture — future enhancement
- Actual vehicle expense method (most users use standard mileage; if Dave
  wants the alternative, that's a separate phase)

### Backend changes

**Schema.**

```python
BusinessTrip:
    vehicle_record_id: str
    date: date                     # primary date (start if multi-day)
    end_date: Optional[date]       # for multi-day trips
    start_mileage: Optional[int]   # odometer at start
    end_mileage: Optional[int]     # odometer at end
    miles: float                   # business miles (computed or entered)
    purpose: str                   # "Client visit", "Pickup parts", etc.
    client: Optional[str]          # CRS client name
    start_location: Optional[str]
    end_location: Optional[str]
    notes: Optional[str]
    is_round_trip: bool            # default True
    tax_year: int                  # derived from date; indexed
```

Stored as `record_type='business_trip'` in `structured_records`.

**IRS rate table.**

`api/data/irs_mileage_rates.py`:

```python
# IRS standard mileage rates (business use). Verify each year against
# https://www.irs.gov/tax-professionals/standard-mileage-rates
RATES = {
  2022: 0.585,   # Jan–Jun
  # 2022 had a mid-year change: 0.625 Jul–Dec — handle as edge case
  2023: 0.655,
  2024: 0.670,
  2025: 0.700,
  # 2026: TBD — IRS publishes in late prior year
}

def rate_for_date(d: date) -> float | None:
    year = d.year
    if year in RATES: return RATES[year]
    return None  # frontend prompts user to enter the current rate
```

The system should never silently use a stale rate. If a year's rate isn't
in the table, the report flow prompts the user to enter it (and writes it
back to the table for future use).

**Endpoints.**

- `POST /api/trips` — create
- `PATCH /api/trips/{id}` — edit
- `DELETE /api/trips/{id}` — delete
- `GET /api/trips?vehicle_id=&tax_year=&from=&to=` — list with filters
- `GET /api/trips/report?tax_year=2025&vehicle_id=` — JSON report
- `GET /api/trips/report.pdf?tax_year=2025&vehicle_id=` — PDF export
- `GET /api/irs-mileage-rates` — current rate table
- `POST /api/irs-mileage-rates` — body `{year: int, rate: float}` to add
  a missing year

**Report structure.**

```json
{
  "tax_year": 2025,
  "vehicle": { "year": 2018, "make": "Toyota", "model": "Sienna", "vin": "..." },
  "period": { "from": "2025-01-01", "to": "2025-12-31" },
  "trip_count": 87,
  "total_business_miles": 4234.5,
  "irs_rate_used": 0.700,
  "total_deduction": 2964.15,
  "by_quarter": {
    "Q1": { "miles": 1102, "deduction": 771.40 },
    ...
  },
  "by_client": {
    "Smithco": { "miles": 1430, "deduction": 1001.00 },
    ...
  },
  "trips": [ ... ]  // full list, sorted by date
}
```

### Frontend changes

**Trip log section on vehicle page.**

New tab/section: "Business mileage." Lists trips for this vehicle, filtered
by tax year (year dropdown, defaults to current). Columns: date, miles,
purpose, client, deduction (computed at row level using the year's rate).

**Log a trip form.**

Drawer with fields above. Two modes:
- **Quick:** date, miles, purpose, client (single line of input each)
- **Detailed:** all fields including locations and odometer readings

Round-trip toggle: when on, miles is the full there-and-back number. Just
a flag, doesn't change math, useful for the user.

**Reports view.**

A new top-level section under the Auto page header — "Reports" — that opens
a side panel:
- Tax year selector
- Vehicle filter (all or specific)
- Quarter filter (all or Q1–Q4)
- Generated summary at the top (totals, rate, deduction)
- Trip table below
- "Export PDF" button

**PDF format.**

Single-page summary + multi-page trip detail. Should be a clean,
IRS-compliant format that Dave can hand to an accountant or attach to a
tax return. Headers: business name (Castle Rock Sky), tax year, vehicle
ID, generated date.

### Implementation notes

- IRS auditing for business mileage focuses on contemporaneousness — logs
  written close to when trips happened. Encourage logging promptly via a
  Coach nudge in Phase 10. The system should record `created_at` on every
  trip; later phases could compute and surface "average days from trip to
  log" as a self-discipline signal.
- The 2022 mid-year rate change is real and a pain. The rate-lookup
  function should handle date-based rate selection within a year, not just
  year-based — design for it now even if the current table only has the
  one such case.
- Trip miles can be entered directly OR computed from start/end mileage.
  If both are provided, prefer entered miles but warn if there's a
  >10% discrepancy.
- This phase intentionally doesn't depend on Phase 7 (recalls) or Phase 8
  (mileage trends). It can ship in parallel with those if useful.
- A future enhancement: pull from the existing time_series mileage data
  to suggest trips — "Between Jan 10 and Jan 11 you drove 84 miles, was
  that business?" Defer for now; trip entry is manual.

### Testing checklist

- [ ] Manual trip creation works with required fields
- [ ] Trip creation in tax year with missing rate prompts to enter rate
- [ ] Rate entry writes back to rate table and persists
- [ ] Report generation returns correct totals for a vehicle with many
      trips across multiple quarters
- [ ] Quarterly breakdown matches sum of trips in each quarter
- [ ] Per-client breakdown sums correctly
- [ ] PDF export renders cleanly with summary + trip detail
- [ ] Tax year filter correctly bounds results
- [ ] Editing a trip recalculates the deduction
- [ ] Deleting a trip removes it from reports
- [ ] 2022 mid-year rate change handled correctly (if testing with 2022 data)
- [ ] Trip with mismatched odometer vs entered miles surfaces a warning

---

## Phase 10: Cross-System Integration

| Field | Value |
|-------|-------|
| **Goal** | Wire the Auto page into Coach, Tempo, Action items, and (optionally) QBO |
| **Scope** | Briefing routing, calendar event creation, action item enhancements, optional QBO sync |
| **Prerequisites** | Phases 1–9 |
| **Primary files** | Coach SOUL.md, Tempo SOUL.md, action item routing |
| **Out of scope** | Discord agent direct integration (Auto page is web-only — surface via existing channels) |

### Goal

A vehicle is rarely the thing Dave is thinking about in a given moment.
The Auto page surfaces information when he goes looking, but the rest of
the LifeOS ecosystem should bring auto-domain urgency to him via the
channels he already checks (Coach morning briefing, Tempo calendar, Action
items, QBO for business expenses).

### Scope

In:
- Coach morning briefing surfaces auto urgencies (registration expiring,
  overdue maintenance, open recalls)
- Tempo calendar gets all-day events for registration renewals,
  inspection due dates, and scheduled maintenance with date-based
  intervals
- Action items from auto domain link back to the vehicle page
- Coach nudge for business trip logging if mileage cadence suggests
  unlogged business travel (optional, behind a setting)
- Optional: QBO expense entry for service records marked as business
  deductions

Out:
- Per-agent direct integration with the Auto page (HaloBot, MealBot, etc.
  don't need vehicle data)
- Real-time push (existing channels are sufficient)

### Coach integration

Update Coach SOUL.md to include auto-domain awareness:

```markdown
## Auto Domain Awareness

In morning briefings, check the auto domain for urgent items:
- Registration expiring within 30 days → mention by vehicle name
- Maintenance overdue (any action items in `auto` domain with `due_date < today`) → mention
- Open recalls → mention severity
- Today is a scheduled maintenance day (cron or date-based) → remind

Format: one line per item, lead with vehicle name.

Example:
"Auto: Sienna registration expires in 12 days. Dakota oil change is overdue."

If no urgent auto items, don't mention auto in the briefing at all.

Never share specific cost data or service receipts in the briefing — those
stay in the Auto page.
```

### Tempo integration

Create calendar events for date-based auto deadlines:

- Registration renewals → all-day event 30 days before expiration, with
  reminder 7 days before and 1 day before
- Inspection / emissions due → all-day event on due date
- Date-based maintenance schedules (e.g. "every 12 months") with a
  `next_due_date` → all-day event on that date

When a renewal is logged (updated `registration_expiration`), update the
calendar event accordingly. When a schedule is deleted, remove the
associated calendar events.

Tempo SOUL.md addition:

```markdown
## Auto Domain Events

Auto-domain events arrive as all-day reminders. Treat them like other
domain-sourced events — they show in the calendar but Dave can't edit
them directly (they're source-of-truth in the Auto page). If Dave asks to
move or delete one, redirect: "That event is from the Auto page — change
it there and it'll update here."
```

### Action item enhancements

Auto-domain action items should:
- Include the vehicle name in the title (e.g. "Sienna: Oil change due")
- Include a `vehicle_id` in metadata so the action card can deep-link to
  the vehicle page
- Show a "View vehicle" button on the action item card

### Business mileage nudge (optional)

Coach could surface a weekly prompt: "You drove ~300 miles last week — any
of it business travel for CRS?" — drives Dave to the trip log if so. Gate
behind a setting in case it's annoying.

### Optional: QBO integration

For service records marked `is_business_deduction` (new field) or trips
generated via Phase 9 reports:

- One-click export to QBO as a categorized expense
- Auto-categorize: trips → "Vehicle: Mileage" expense account; service
  receipts → "Vehicle: Maintenance"

This depends on the existing QBO OAuth being configured (which it is per
the user memory). Defer if it adds friction; the PDF report from Phase 9
is enough for the accountant to handle this.

### Implementation notes

- Coach already reads from a structured "today's signals" data source.
  Add auto-domain signals to whatever that aggregation is.
- Tempo calendar events should be tagged with `source: 'auto'` so they
  can be filtered or deleted in bulk if the integration is disabled.
- The business mileage nudge requires comparing logged trips against
  total mileage delta over the same period — a useful heuristic, not a
  guarantee.

### Testing checklist

- [ ] Coach briefing includes auto domain items when urgent
- [ ] Coach briefing omits auto domain when nothing urgent
- [ ] Registration renewal creates a Tempo calendar event
- [ ] Updating registration_expiration updates the calendar event
- [ ] Date-based maintenance schedules appear on Tempo calendar
- [ ] Action items deep-link to the vehicle page
- [ ] Business mileage nudge fires when total mileage exceeds logged
      trips (if setting enabled)
- [ ] Disabling Tempo integration removes all auto-sourced events cleanly
- [ ] QBO export (if implemented) creates a categorized expense

---

## Appendix A: Schema Reference

Current schemas (from `api/schemas/auto.py`):

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

New schemas introduced in this spec:

- `Phase 2`: adds `status` lifecycle values, `merged_into_vehicle_id` to Vehicle
- `Phase 3`: adds `predicted_due_date` to MaintenanceSchedule
- `Phase 4`: adds `category` to ServiceRecord
- `Phase 7`: new `VehicleRecall` type
- `Phase 9`: new `BusinessTrip` type

---

## Appendix B: Deferred / Future Enhancements

These came up during design but aren't part of the 10-phase build:

- **Mobile PWA odometer capture** — Snap a photo of the odometer, OCR
  the number, log mileage. Aligns with the broader LifeOS Phase 4 mobile
  capture work. Implement when the PWA layer is ready.
- **VIN-based duplicate detection** — When adding a vehicle, check VIN
  against existing records and suggest merge.
- **Per-make/model maintenance templates** — More specific than the
  generic templates in Phase 3 (e.g. Toyota timing belt at 90k, Subaru
  CVT fluid intervals).
- **Fuel log + MPG tracking** — Only valuable if Dave would log fill-ups
  regularly. Defer until there's a clear demand.
- **Tire age tracking** — Separate from service records, tracks tire
  purchase date and expected lifespan independently.
- **Trip auto-detection from mileage gaps** — Phase 9 mentions this in
  passing. Compare time_series_metrics mileage jumps against logged
  trips; surface gaps for the user to triage.
- **Map view of service providers** — Geographic clustering of where
  services have been performed.
- **Comparative fleet analytics** — When the fleet has 3+ active
  vehicles, a comparison view (cost/mile, reliability, etc.) becomes
  interesting.
- **Insurance claims linkage** — When the insurance domain page exists,
  link claims to the vehicle they relate to.
- **State-specific inspection/emissions handling** — Colorado has
  emissions testing requirements for some vehicles. Could be auto-tracked.

---

## Appendix C: Recommended Build Order Recap

For the minimum viable redesign that delivers most of the value:

1. **Phase 1** (drill-in foundation) — unblocks everything visual
2. **Phase 2** (CRUD + merge) — fixes the duplicate-data problem
3. **Phase 3** (maintenance schedules) — makes the action-item engine useful
4. **Phase 7** (recalls) — surfaces a class of urgency that's invisible today

That four-phase subset transforms the page from inventory to working tool.
Phases 4, 5, 6, 8 layer polish and analytics. Phase 9 is the CRS business
addition. Phase 10 wires everything into the rest of LifeOS.

Ship phases sequentially or in parallel where dependencies allow. Phase 9
can ship in parallel with 4–8 since it doesn't depend on them.

---

## End of Spec
