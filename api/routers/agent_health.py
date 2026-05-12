"""
routers/agent_health.py — HealthBot agent API (Phase 5).

All routes require X-Agent-Key with the 'medical' domain in its allow-list.
Subjects are addressed by NAME (case-insensitive substring match) so HealthBot
doesn't have to track UUIDs. The match falls back to the primary subject when
nothing else fits, which is the right thing for personal health bots.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from agent_auth import require_agent_domain
from database import get_pool
from trends import compute_trend

router = APIRouter(prefix="/api/agent/health", tags=["agent-health"])

_require = require_agent_domain("medical")


# ── Subject resolution by name ──────────────────────────────────────────

async def _resolve_subject_by_name(name: str) -> str:
    """Map an agent-supplied subject name to a subject UUID.

    Strategy: case-insensitive LIKE match. If none, fall back to primary subject.
    """
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
        raise HTTPException(404, f"Subject '{name}' not found and no primary subject exists")
    return str(row["id"])


# ── Metrics ─────────────────────────────────────────────────────────────

class MetricBody(BaseModel):
    subject: str
    type: str
    value: Optional[float] = None
    value_text: Optional[str] = None
    notes: Optional[str] = None
    recorded_at: Optional[datetime] = None


@router.get("/metrics")
async def list_metrics(
    subject: str = Query(...),
    type: str = Query(..., alias="type"),
    days: int = Query(30, ge=1, le=3650),
    _: dict = Depends(_require),
):
    sid = await _resolve_subject_by_name(subject)
    pool = get_pool()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, value_numeric, value_text, recorded_at, source, notes
            FROM time_series_metrics
            WHERE subject_id = $1 AND metric_type = $2 AND recorded_at >= $3
            ORDER BY recorded_at DESC
        """, uuid.UUID(sid), type, since)

    return {
        "data": [
            {
                "id": r["id"],
                "value": float(r["value_numeric"]) if r["value_numeric"] is not None else None,
                "value_text": r["value_text"],
                "recorded_at": r["recorded_at"].isoformat(),
                "source": r["source"],
                "notes": r["notes"],
            }
            for r in rows
        ],
        "meta": {"subject_id": sid, "metric_type": type, "days": days},
    }


@router.post("/metrics")
async def create_metric(body: MetricBody, _: dict = Depends(_require)):
    if body.value is None and not body.value_text:
        raise HTTPException(400, "Provide value or value_text")

    sid = await _resolve_subject_by_name(body.subject)
    recorded = body.recorded_at or datetime.now(timezone.utc)

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO time_series_metrics
                (subject_id, metric_type, value_numeric, value_text, recorded_at, source, notes)
            VALUES ($1, $2, $3, $4, $5, 'agent_api', $6)
            RETURNING id, recorded_at
        """, uuid.UUID(sid), body.type, body.value, body.value_text, recorded, body.notes)

    return {
        "data": {
            "id": row["id"],
            "subject_id": sid,
            "metric_type": body.type,
            "value": body.value,
            "value_text": body.value_text,
            "recorded_at": row["recorded_at"].isoformat(),
        }
    }


# ── Blood pressure convenience ──────────────────────────────────────────

class BPBody(BaseModel):
    subject: str
    systolic: float
    diastolic: float
    pulse: Optional[float] = None
    notes: Optional[str] = None
    recorded_at: Optional[datetime] = None


@router.post("/bp")
async def log_bp(body: BPBody, _: dict = Depends(_require)):
    sid = await _resolve_subject_by_name(body.subject)
    recorded = body.recorded_at or datetime.now(timezone.utc)

    pool = get_pool()
    rows: list[dict] = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            for metric_type, value in (
                ("blood_pressure_systolic", body.systolic),
                ("blood_pressure_diastolic", body.diastolic),
                ("heart_rate_resting", body.pulse),
            ):
                if value is None:
                    continue
                row = await conn.fetchrow("""
                    INSERT INTO time_series_metrics
                        (subject_id, metric_type, value_numeric, recorded_at, source, notes)
                    VALUES ($1, $2, $3, $4, 'agent_api', $5)
                    RETURNING id
                """, uuid.UUID(sid), metric_type, value, recorded, body.notes)
                rows.append({"id": row["id"], "metric_type": metric_type, "value": value})

    return {
        "data": {
            "subject_id": sid,
            "recorded_at": recorded.isoformat(),
            "metrics": rows,
        }
    }


# ── Medications ─────────────────────────────────────────────────────────

@router.get("/medications")
async def list_medications(
    subject: str = Query(...),
    active: bool = Query(True),
    _: dict = Depends(_require),
):
    sid = await _resolve_subject_by_name(subject)
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, data, valid_from, valid_to, next_action_date, next_action_description, updated_at
            FROM structured_records
            WHERE deleted_at IS NULL
              AND record_type = 'medication'
              AND subject_id = $1
            ORDER BY updated_at DESC
        """, uuid.UUID(sid))

    items = []
    for r in rows:
        data = r["data"] if isinstance(r["data"], dict) else json.loads(r["data"])
        status = (data.get("status") or "active").lower()
        if active and status != "active":
            continue
        items.append({
            "id": str(r["id"]),
            "name": data.get("name"),
            "dose": data.get("dose"),
            "frequency": data.get("frequency"),
            "time_of_day": data.get("time_of_day"),
            "prescriber": data.get("prescriber"),
            "pharmacy": data.get("pharmacy"),
            "rx_number": data.get("rx_number"),
            "refill_date": data.get("refill_date"),
            "refills_remaining": data.get("refills_remaining"),
            "indication": data.get("indication"),
            "status": status,
            "notes": data.get("notes"),
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        })

    return {"data": items, "meta": {"subject_id": sid, "active_only": active}}


class DoseBody(BaseModel):
    timestamp: Optional[datetime] = None
    status: str  # taken, missed, late, skipped
    notes: Optional[str] = None


@router.post("/medications/{record_id}/taken")
async def log_dose(record_id: str, body: DoseBody, _: dict = Depends(_require)):
    if body.status not in ("taken", "missed", "late", "skipped"):
        raise HTTPException(400, "status must be one of: taken, missed, late, skipped")

    try:
        rid = uuid.UUID(record_id)
    except ValueError:
        raise HTTPException(400, "Invalid medication record id")

    pool = get_pool()
    async with pool.acquire() as conn:
        med = await conn.fetchrow("""
            SELECT subject_id FROM structured_records
            WHERE id = $1 AND record_type = 'medication' AND deleted_at IS NULL
        """, rid)
        if not med:
            raise HTTPException(404, "Medication not found")

        recorded = body.timestamp or datetime.now(timezone.utc)
        row = await conn.fetchrow("""
            INSERT INTO medication_doses
                (medication_record_id, subject_id, recorded_at, status, notes, source)
            VALUES ($1, $2, $3, $4, $5, 'agent_api')
            RETURNING id, recorded_at
        """, rid, med["subject_id"], recorded, body.status, body.notes)

    return {
        "data": {
            "id": row["id"],
            "medication_record_id": str(rid),
            "status": body.status,
            "recorded_at": row["recorded_at"].isoformat(),
        }
    }


@router.get("/medications/adherence")
async def adherence(
    subject: str = Query(...),
    days: int = Query(30, ge=1, le=3650),
    _: dict = Depends(_require),
):
    sid = await _resolve_subject_by_name(subject)
    since = datetime.now(timezone.utc) - timedelta(days=days)

    pool = get_pool()
    async with pool.acquire() as conn:
        # Per-medication breakdown
        per_med = await conn.fetch("""
            SELECT
                sr.id,
                sr.data->>'name' AS name,
                COUNT(*) FILTER (WHERE md.status = 'taken')   AS taken,
                COUNT(*) FILTER (WHERE md.status = 'late')    AS late,
                COUNT(*) FILTER (WHERE md.status = 'missed')  AS missed,
                COUNT(*) FILTER (WHERE md.status = 'skipped') AS skipped,
                COUNT(*) AS total
            FROM structured_records sr
            LEFT JOIN medication_doses md
                   ON md.medication_record_id = sr.id AND md.recorded_at >= $2
            WHERE sr.deleted_at IS NULL
              AND sr.record_type = 'medication'
              AND sr.subject_id = $1
            GROUP BY sr.id, sr.data
            ORDER BY total DESC
        """, uuid.UUID(sid), since)

        # Overall
        total_row = await conn.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'taken')   AS taken,
                COUNT(*) FILTER (WHERE status = 'late')    AS late,
                COUNT(*) FILTER (WHERE status = 'missed')  AS missed,
                COUNT(*) FILTER (WHERE status = 'skipped') AS skipped,
                COUNT(*) AS total
            FROM medication_doses
            WHERE subject_id = $1 AND recorded_at >= $2
        """, uuid.UUID(sid), since)

    def _pct(taken: int, total: int) -> Optional[float]:
        if not total:
            return None
        return round((taken / total) * 100, 1)

    return {
        "data": {
            "overall": {
                "taken": total_row["taken"],
                "late": total_row["late"],
                "missed": total_row["missed"],
                "skipped": total_row["skipped"],
                "total": total_row["total"],
                "adherence_pct": _pct(total_row["taken"] + total_row["late"], total_row["total"]),
            },
            "per_medication": [
                {
                    "id": str(r["id"]),
                    "name": r["name"],
                    "taken": r["taken"],
                    "late": r["late"],
                    "missed": r["missed"],
                    "skipped": r["skipped"],
                    "total": r["total"],
                    "adherence_pct": _pct((r["taken"] or 0) + (r["late"] or 0), r["total"] or 0),
                }
                for r in per_med
            ],
        },
        "meta": {"subject_id": sid, "days": days},
    }


# ── Providers ───────────────────────────────────────────────────────────

@router.get("/providers")
async def list_providers(subject: str = Query(...), _: dict = Depends(_require)):
    sid = await _resolve_subject_by_name(subject)
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, data, updated_at
            FROM structured_records
            WHERE deleted_at IS NULL AND record_type = 'provider' AND subject_id = $1
            ORDER BY updated_at DESC
        """, uuid.UUID(sid))

    out = []
    for r in rows:
        data = r["data"] if isinstance(r["data"], dict) else json.loads(r["data"])
        out.append({
            "id": str(r["id"]),
            "name": data.get("name"),
            "specialty": data.get("specialty"),
            "practice": data.get("practice"),
            "phone": data.get("phone"),
            "portal_url": data.get("portal_url"),
            "next_appointment": data.get("next_appointment"),
            "notes": data.get("notes"),
        })
    return {"data": out, "meta": {"subject_id": sid}}


# ── Trends ──────────────────────────────────────────────────────────────

@router.get("/trends")
async def trends(
    subject: str = Query(...),
    type: str = Query(...),
    period: str = Query("weekly"),
    _: dict = Depends(_require),
):
    sid = await _resolve_subject_by_name(subject)
    try:
        result = await compute_trend(subject_id=sid, metric_type=type, period=period)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"data": result}
