"""
recurrences.py — Compute next due dates for recurring financial obligations
and ensure a pending action_item exists for them.

Used by the AI ingestion pipeline (when a financial record is extracted) and
by the records router (when a user creates one manually). Idempotent: calling
twice with no intervening completion does NOT stack duplicate action items.
"""

import logging
import uuid
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


def _next_monthly_due(due_day: int, today: Optional[date] = None) -> date:
    """Return the next occurrence of `due_day` of the month, on or after today."""
    today = today or date.today()
    day = max(1, min(31, int(due_day)))

    # Try this month first; if the day has passed, jump to next month. Clamp
    # to the last day of the target month so 31-of-Feb doesn't crash.
    def _safe(year: int, month: int, day: int) -> date:
        # Last day of month
        if month == 12:
            first_next = date(year + 1, 1, 1)
        else:
            first_next = date(year, month + 1, 1)
        last = (first_next - timedelta(days=1)).day
        return date(year, month, min(day, last))

    candidate = _safe(today.year, today.month, day)
    if candidate < today:
        m = today.month + 1
        y = today.year + (1 if m > 12 else 0)
        if m > 12:
            m = 1
        candidate = _safe(y, m, day)
    return candidate


def _parse_date(value) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def _next_due_for(record_type: str, data: dict) -> Optional[date]:
    """Inspect a record's `data` dict and return the next due date, if any."""
    if not isinstance(data, dict):
        return None

    today = date.today()

    if record_type == "credit_account":
        d = _parse_date(data.get("payment_due_date"))
        return d if (d and d >= today) else None

    if record_type == "loan":
        day = data.get("payment_due_day")
        if isinstance(day, int) and 1 <= day <= 31:
            return _next_monthly_due(day, today)
        return None

    if record_type == "recurring_expense":
        freq = (data.get("frequency") or "monthly").lower()
        day = data.get("due_day")
        if freq == "monthly" and isinstance(day, int) and 1 <= day <= 31:
            return _next_monthly_due(day, today)
        return None

    if record_type == "appliance":
        # Service due is informational; only generate an action item if it's
        # in the next 90 days or already overdue (within 90 days back).
        d = _parse_date(data.get("next_service_due"))
        if d and (today - timedelta(days=90)) <= d <= (today + timedelta(days=90)):
            return d
        return None

    if record_type == "home_maintenance_schedule":
        d = _parse_date(data.get("next_due"))
        if d and (today - timedelta(days=30)) <= d <= (today + timedelta(days=120)):
            return d
        return None

    if record_type == "maintenance_schedule":
        d = _parse_date(data.get("next_due_date"))
        if d and (today - timedelta(days=30)) <= d <= (today + timedelta(days=120)):
            return d
        return None

    if record_type == "vehicle":
        # Registration renewal action item, 60 days before expiration.
        exp = _parse_date(data.get("registration_expiration"))
        if exp:
            lead = exp - timedelta(days=60)
            if today >= lead:
                return exp
        return None

    if record_type == "pet_vaccination":
        # Vaccination renewals — surface 60 days before next_due (and overdue).
        d = _parse_date(data.get("next_due"))
        if d:
            lead = d - timedelta(days=60)
            if today >= lead:
                return d
        return None

    if record_type == "preventative_schedule":
        # Flea/tick/heartworm — usually monthly. Surface as soon as due.
        d = _parse_date(data.get("next_due"))
        if d and (today - timedelta(days=30)) <= d <= (today + timedelta(days=60)):
            return d
        return None

    if record_type == "pet_medication":
        # Pet med refills, identical pattern to medication.refill_date.
        d = _parse_date(data.get("refill_date"))
        if d and (today - timedelta(days=14)) <= d <= (today + timedelta(days=60)):
            return d
        return None

    if record_type == "insurance_policy":
        # Renewal action 30 days before expiration.
        if data.get("auto_renew") is True:
            return None
        exp = _parse_date(data.get("expiration_date"))
        if exp and today >= exp - timedelta(days=30):
            return exp
        return None

    if record_type == "identity_document":
        # Passport renewals especially want a long lead time. Surface 180 days
        # before for passports, 60 for other docs.
        exp = _parse_date(data.get("expiration_date"))
        if not exp:
            return None
        doc_type = (data.get("document_type") or "").lower()
        lead = 180 if doc_type == "passport" else 60
        if today >= exp - timedelta(days=lead):
            return exp
        return None

    if record_type == "tax_item":
        # Skip already-resolved items (paid, filed, refunded).
        status = (data.get("status") or "pending").lower()
        if status not in ("pending",):
            return None
        d = _parse_date(data.get("due_date"))
        if not d:
            return None
        # Surface 30 days before deadline (tax deadlines are mostly known well
        # in advance — earlier than that is just nag).
        if today >= d - timedelta(days=30):
            return d
        return None

    return None


def _action_title_for(record_type: str, data: dict, amount: Optional[float]) -> str:
    if record_type == "credit_account":
        return f"Pay {data.get('creditor') or 'credit card'}"
    if record_type == "loan":
        return f"Loan payment: {data.get('lender') or 'loan'}"
    if record_type == "recurring_expense":
        return f"{data.get('name') or 'expense'} due"
    if record_type == "appliance":
        return f"Service: {data.get('name') or 'appliance'}"
    if record_type == "home_maintenance_schedule":
        return data.get("task") or "Home maintenance"
    if record_type == "maintenance_schedule":
        svc = data.get("service_type") or "Maintenance"
        # Vehicle name (if the caller resolved it) lands ahead of the
        # service type so the action card reads "Sienna: Oil change" — Phase
        # 10 cross-system integration; the title also drives the calendar
        # event and Coach briefing text.
        vehicle_name = data.get("__vehicle_name") if isinstance(data, dict) else None
        if vehicle_name:
            return f"{vehicle_name}: {svc}"
        return f"Vehicle: {svc}"
    if record_type == "vehicle":
        return f"Renew registration: {data.get('year') or ''} {data.get('make') or ''} {data.get('model') or ''}".strip()
    if record_type == "pet_vaccination":
        return f"Vaccination due: {data.get('name') or 'vaccine'}"
    if record_type == "preventative_schedule":
        return f"{(data.get('product') or data.get('type') or 'preventative').title()} due"
    if record_type == "pet_medication":
        return f"Refill (pet): {data.get('name') or 'medication'}"
    if record_type == "insurance_policy":
        return f"Renew {data.get('policy_type') or 'insurance'}: {data.get('carrier') or ''}".strip()
    if record_type == "identity_document":
        return f"Renew {data.get('document_type') or 'document'}"
    if record_type == "tax_item":
        desc = data.get("description") or "tax item"
        year = data.get("tax_year")
        return f"Tax: {desc}" + (f" (TY {year})" if year else "")
    return data.get("name") or "Action"


_DOMAIN_FOR_RECORD_TYPE = {
    "credit_account": "financial",
    "loan": "financial",
    "recurring_expense": "financial",
    "appliance": "home",
    "home_maintenance_schedule": "home",
    "maintenance_schedule": "auto",
    "vehicle": "auto",
    "pet_vaccination": "vet",
    "preventative_schedule": "vet",
    "pet_medication": "vet",
    "insurance_policy": "insurance",
    "identity_document": "legal",
    "tax_item": "tax",
}


async def ensure_recurring_action_item(
    conn,
    *,
    record_type: str,
    record_id: uuid.UUID,
    data: dict,
    subject_id: Optional[uuid.UUID],
    source_document_id: Optional[uuid.UUID] = None,
    vehicle_name: Optional[str] = None,
    extra_metadata: Optional[dict] = None,
) -> Optional[int]:
    """Create a pending action_item for the next due date if one doesn't exist.

    Returns the action_item.id if created, None if skipped.

    Skipped cases:
      - The record type isn't recurring.
      - autopay = true (we still surface in upcoming-payments list, but don't
        nag with an action item that the user can't action).
      - A pending action item already exists for this record with a due_date
        on or after today.
    """
    next_due = _next_due_for(record_type, data)
    if not next_due:
        return None
    if data.get("autopay") is True:
        return None

    today = date.today()

    # Bail if we already have an open action item for this record that's still
    # in the future.
    existing = await conn.fetchval("""
        SELECT 1 FROM action_items
        WHERE source_record_id = $1
          AND status = 'pending'
          AND deleted_at IS NULL
          AND (due_date IS NULL OR due_date >= $2)
        LIMIT 1
    """, record_id, today)
    if existing:
        return None

    amount = data.get("amount") or data.get("minimum_payment") or data.get("monthly_payment")
    # Sneak the resolved vehicle name into the data passed to the title
    # builder. The dunder key avoids colliding with any real schema field.
    title_data = data
    if vehicle_name:
        title_data = {**data, "__vehicle_name": vehicle_name}
    title = _action_title_for(record_type, title_data, amount)
    description_parts = []
    if amount is not None:
        try:
            description_parts.append(f"${float(amount):,.2f}")
        except (TypeError, ValueError):
            pass
    if data.get("autopay") is True:
        description_parts.append("(autopay)")
    description = " ".join(description_parts) or None

    domain = _DOMAIN_FOR_RECORD_TYPE.get(record_type)
    recurrence_rule = None
    if record_type in ("loan", "recurring_expense"):
        recurrence_rule = "monthly"
    elif record_type in ("appliance", "home_maintenance_schedule", "maintenance_schedule"):
        # Recur via the source record's interval (caller updates last_completed
        # / last_service after the action is marked done, so the next call to
        # this function will compute a fresh due date).
        recurrence_rule = "interval"

    # Action priority bumps when something is overdue.
    priority = "medium"
    if next_due < today:
        priority = "high"

    # Phase 10: optional metadata blob lets callers attach deep-link hints
    # (e.g. vehicle_id) so the UI can render "View vehicle" on the action
    # card. Defaults to {} so the column is never null.
    metadata = dict(extra_metadata or {})

    new_id = await conn.fetchval("""
        INSERT INTO action_items
            (domain, subject_id, title, description, due_date,
             source_type, source_document_id, source_record_id,
             priority, recurrence_rule, metadata)
        VALUES ($1, $2, $3, $4, $5, 'recurring', $6, $7, $8, $9, $10)
        RETURNING id
    """,
        domain,
        subject_id,
        title,
        description,
        next_due,
        source_document_id,
        record_id,
        priority,
        recurrence_rule,
        metadata,
    )
    return new_id
