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


def _next_due_for(record_type: str, data: dict) -> Optional[date]:
    """Inspect a record's `data` dict and return the next due date, if any."""
    if not isinstance(data, dict):
        return None

    today = date.today()

    if record_type == "credit_account":
        # Prefer an explicit upcoming payment_due_date if it's still in the
        # future; otherwise no recurrence info available.
        raw = data.get("payment_due_date")
        if raw:
            try:
                d = date.fromisoformat(str(raw))
                if d >= today:
                    return d
            except ValueError:
                pass
        return None

    if record_type == "loan":
        # Prefer specific payment_due_day; fall back to a "1st of next month"
        # heuristic only if monthly_payment is set (so we know it recurs).
        day = data.get("payment_due_day")
        if isinstance(day, int) and 1 <= day <= 31:
            return _next_monthly_due(day, today)
        return None

    if record_type == "recurring_expense":
        freq = (data.get("frequency") or "monthly").lower()
        day = data.get("due_day")
        if freq == "monthly" and isinstance(day, int) and 1 <= day <= 31:
            return _next_monthly_due(day, today)
        # Quarterly/yearly omitted for now — AI will set due_day for monthly
        # cases, which covers most real-world recurring expenses.
        return None

    return None


def _action_title_for(record_type: str, data: dict, amount: Optional[float]) -> str:
    name = (
        data.get("name")
        or data.get("creditor")
        or data.get("lender")
        or "Payment"
    )
    if record_type == "credit_account":
        return f"Pay {name}"
    if record_type == "loan":
        return f"Loan payment: {name}"
    if record_type == "recurring_expense":
        return f"{name} due"
    return name


async def ensure_recurring_action_item(
    conn,
    *,
    record_type: str,
    record_id: uuid.UUID,
    data: dict,
    subject_id: Optional[uuid.UUID],
    source_document_id: Optional[uuid.UUID] = None,
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
    title = _action_title_for(record_type, data, amount)
    description_parts = []
    if amount is not None:
        try:
            description_parts.append(f"${float(amount):,.2f}")
        except (TypeError, ValueError):
            pass
    if data.get("autopay") is True:
        description_parts.append("(autopay)")
    description = " ".join(description_parts) or None

    new_id = await conn.fetchval("""
        INSERT INTO action_items
            (domain, subject_id, title, description, due_date,
             source_type, source_document_id, source_record_id,
             priority, recurrence_rule)
        VALUES ('financial', $1, $2, $3, $4, 'recurring', $5, $6, 'medium', $7)
        RETURNING id
    """,
        subject_id,
        title,
        description,
        next_due,
        source_document_id,
        record_id,
        "monthly" if record_type in ("loan", "recurring_expense") else None,
    )
    return new_id
