"""
routers/agent_finance.py — FinanceBot agent API (Phase 6).

All routes require X-Agent-Key with the 'financial' domain.
Subjects default to the primary subject; an optional `subject` query param
overrides that with a name match.
"""

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from agent_auth import require_agent_domain
from database import get_pool
from recurrences import _next_due_for

router = APIRouter(prefix="/api/agent/finance", tags=["agent-finance"])

_require = require_agent_domain("financial")


# ── Subject resolution ──────────────────────────────────────────────────

async def _resolve_subject(name: Optional[str]) -> str:
    pool = get_pool()
    async with pool.acquire() as conn:
        if name:
            row = await conn.fetchrow(
                "SELECT id FROM subjects WHERE deleted_at IS NULL "
                "AND LOWER(name) LIKE $1 ORDER BY is_primary DESC LIMIT 1",
                f"%{name.lower()}%",
            )
            if row:
                return str(row["id"])
        row = await conn.fetchrow(
            "SELECT id FROM subjects WHERE deleted_at IS NULL AND is_primary = true LIMIT 1"
        )
    if not row:
        raise HTTPException(404, "No subject found")
    return str(row["id"])


def _data(row) -> dict:
    return row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])


async def _records(conn, sid: str, record_type: str) -> list:
    return await conn.fetch("""
        SELECT id, record_type, data, source_document_id, updated_at
        FROM structured_records
        WHERE deleted_at IS NULL AND record_type = $1 AND subject_id = $2
        ORDER BY updated_at DESC
    """, record_type, uuid.UUID(sid))


# ── Helpers for upcoming payments ───────────────────────────────────────

def _gather_upcoming(records_by_type: dict, days: int) -> list[dict]:
    today = date.today()
    cutoff = today + timedelta(days=days)
    out: list[dict] = []

    for rtype in ("credit_account", "loan", "recurring_expense"):
        for row in records_by_type.get(rtype, []):
            data = _data(row)
            status = (data.get("status") or "active").lower()
            if status not in ("active",):
                continue
            due = _next_due_for(rtype, data)
            if not due or due > cutoff:
                continue
            amount = (
                data.get("amount")
                or data.get("minimum_payment")
                or data.get("monthly_payment")
            )
            name = (
                data.get("name")
                or data.get("creditor")
                or data.get("lender")
                or "Payment"
            )
            out.append({
                "record_id": str(row["id"]),
                "type": rtype,
                "name": name,
                "amount": float(amount) if amount is not None else None,
                "due_date": due.isoformat(),
                "days_until": (due - today).days,
                "autopay": bool(data.get("autopay")),
                "account_last_four": data.get("last_four"),
            })

    out.sort(key=lambda r: r["due_date"])
    return out


# ── Routes ──────────────────────────────────────────────────────────────

@router.get("/summary")
async def summary(
    subject: Optional[str] = Query(None),
    _: dict = Depends(_require),
):
    sid = await _resolve_subject(subject)
    pool = get_pool()
    async with pool.acquire() as conn:
        records = {
            t: await _records(conn, sid, t)
            for t in ("credit_account", "loan", "recurring_expense", "bank_account")
        }

    total_debt = 0.0
    debt_breakdown = {"credit": 0.0, "loan": 0.0}
    for row in records["credit_account"]:
        bal = _data(row).get("current_balance")
        if isinstance(bal, (int, float)):
            total_debt += float(bal)
            debt_breakdown["credit"] += float(bal)
    for row in records["loan"]:
        bal = _data(row).get("current_balance")
        if isinstance(bal, (int, float)):
            total_debt += float(bal)
            debt_breakdown["loan"] += float(bal)

    monthly_obligations = 0.0
    for row in records["recurring_expense"]:
        d = _data(row)
        if (d.get("status") or "active") != "active":
            continue
        if (d.get("frequency") or "monthly").lower() != "monthly":
            continue
        amt = d.get("amount")
        if isinstance(amt, (int, float)):
            monthly_obligations += float(amt)
    for row in records["loan"]:
        amt = _data(row).get("monthly_payment")
        if isinstance(amt, (int, float)):
            monthly_obligations += float(amt)
    for row in records["credit_account"]:
        amt = _data(row).get("minimum_payment")
        if isinstance(amt, (int, float)):
            monthly_obligations += float(amt)

    total_credit_limit = sum(
        float(_data(r).get("credit_limit") or 0)
        for r in records["credit_account"]
        if isinstance(_data(r).get("credit_limit"), (int, float))
    )
    utilization = None
    if total_credit_limit > 0:
        utilization = round(
            (debt_breakdown["credit"] / total_credit_limit) * 100, 1
        )

    upcoming_7d = _gather_upcoming(records, days=7)

    return {
        "data": {
            "subject_id": sid,
            "total_debt": round(total_debt, 2),
            "debt_breakdown": {k: round(v, 2) for k, v in debt_breakdown.items()},
            "total_credit_limit": round(total_credit_limit, 2),
            "credit_utilization_pct": utilization,
            "monthly_obligations": round(monthly_obligations, 2),
            "upcoming_payments_7d": upcoming_7d,
            "account_counts": {
                "credit": len(records["credit_account"]),
                "loan": len(records["loan"]),
                "recurring_expense": len(records["recurring_expense"]),
                "bank": len(records["bank_account"]),
            },
        }
    }


@router.get("/debts")
async def debts(
    subject: Optional[str] = Query(None),
    type: str = Query("all", regex="^(all|credit|loan)$"),
    _: dict = Depends(_require),
):
    sid = await _resolve_subject(subject)
    pool = get_pool()
    out: list[dict] = []
    async with pool.acquire() as conn:
        if type in ("all", "credit"):
            for r in await _records(conn, sid, "credit_account"):
                d = _data(r)
                out.append({
                    "record_id": str(r["id"]),
                    "type": "credit",
                    "name": d.get("creditor"),
                    "last_four": d.get("last_four"),
                    "balance": d.get("current_balance"),
                    "limit": d.get("credit_limit"),
                    "minimum_payment": d.get("minimum_payment"),
                    "apr": d.get("apr"),
                    "payment_due_date": d.get("payment_due_date"),
                    "autopay": d.get("autopay"),
                })
        if type in ("all", "loan"):
            for r in await _records(conn, sid, "loan"):
                d = _data(r)
                out.append({
                    "record_id": str(r["id"]),
                    "type": "loan",
                    "name": d.get("lender"),
                    "loan_type": d.get("loan_type"),
                    "balance": d.get("current_balance"),
                    "monthly_payment": d.get("monthly_payment"),
                    "interest_rate": d.get("interest_rate"),
                    "remaining_payments": d.get("remaining_payments"),
                    "payoff_date": d.get("payoff_date"),
                    "autopay": d.get("autopay"),
                })
    return {"data": out, "meta": {"subject_id": sid, "type": type}}


@router.get("/accounts")
async def accounts(
    subject: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    _: dict = Depends(_require),
):
    sid = await _resolve_subject(subject)
    pool = get_pool()
    out: list[dict] = []
    async with pool.acquire() as conn:
        for r in await _records(conn, sid, "bank_account"):
            d = _data(r)
            atype = (d.get("account_type") or "").lower()
            if type and type.lower() not in (atype, "all"):
                if not (type.lower() == "investment" and atype == "investment"):
                    continue
            out.append({
                "record_id": str(r["id"]),
                "type": atype,
                "institution": d.get("institution"),
                "last_four": d.get("last_four"),
                "balance": d.get("balance"),
                "balance_date": d.get("balance_date"),
            })
        if type in (None, "credit", "all"):
            for r in await _records(conn, sid, "credit_account"):
                d = _data(r)
                out.append({
                    "record_id": str(r["id"]),
                    "type": "credit",
                    "institution": d.get("creditor"),
                    "last_four": d.get("last_four"),
                    "balance": d.get("current_balance"),
                    "limit": d.get("credit_limit"),
                    "balance_date": d.get("balance_date"),
                })
    return {"data": out, "meta": {"subject_id": sid, "type": type}}


@router.get("/upcoming-payments")
async def upcoming(
    subject: Optional[str] = Query(None),
    days: int = Query(14, ge=1, le=365),
    _: dict = Depends(_require),
):
    sid = await _resolve_subject(subject)
    pool = get_pool()
    async with pool.acquire() as conn:
        records = {
            t: await _records(conn, sid, t)
            for t in ("credit_account", "loan", "recurring_expense")
        }
    payments = _gather_upcoming(records, days=days)
    return {"data": payments, "meta": {"subject_id": sid, "days": days}}


class BalanceUpdateBody(BaseModel):
    record_id: str
    balance: float
    balance_date: Optional[date] = None
    notes: Optional[str] = None


@router.post("/balance-update")
async def update_balance(body: BalanceUpdateBody, _: dict = Depends(_require)):
    try:
        rid = uuid.UUID(body.record_id)
    except ValueError:
        raise HTTPException(400, "Invalid record_id")

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id, record_type, subject_id, data
            FROM structured_records
            WHERE id = $1 AND deleted_at IS NULL
        """, rid)
        if not row:
            raise HTTPException(404, "Record not found")
        if row["record_type"] not in ("bank_account", "credit_account", "loan"):
            raise HTTPException(
                400,
                f"Record type '{row['record_type']}' does not have a balance",
            )

        data = _data(row)
        bal_date = body.balance_date or date.today()

        if row["record_type"] == "bank_account":
            data["balance"] = body.balance
            data["balance_date"] = bal_date.isoformat()
            metric_type = "bank_account_balance"
        else:
            data["current_balance"] = body.balance
            data["balance_date"] = bal_date.isoformat()
            metric_type = (
                "credit_card_balance"
                if row["record_type"] == "credit_account"
                else "loan_balance"
            )

        await conn.execute(
            "UPDATE structured_records SET data = $1::jsonb WHERE id = $2",
            json.dumps(data, default=str), rid,
        )

        # Trend snapshot
        if row["subject_id"]:
            await conn.execute("""
                INSERT INTO time_series_metrics
                    (subject_id, metric_type, value_numeric, recorded_at, source, notes)
                VALUES ($1, $2, $3, $4, 'agent_api', $5)
            """,
                row["subject_id"],
                metric_type,
                body.balance,
                datetime.combine(bal_date, datetime.min.time(), tzinfo=timezone.utc),
                body.notes,
            )

    return {
        "data": {
            "record_id": str(rid),
            "balance": body.balance,
            "balance_date": bal_date.isoformat(),
        }
    }
