"""
routers/metrics.py — User-facing time-series metric POST + trend lookups.

The list endpoint (GET /api/metrics) lives in routers/system.py and remains
unchanged. This module adds POST and trend endpoints that the medical
dashboard and other domain dashboards rely on.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from database import get_pool
from helpers import audit_log, get_user_email
from trends import compute_trend, latest_metric

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


class MetricCreate(BaseModel):
    subject_id: str
    metric_type: str
    value_numeric: Optional[float] = None
    value_text: Optional[str] = None
    recorded_at: Optional[datetime] = None
    source: Optional[str] = "manual"
    notes: Optional[str] = None


@router.post("")
async def create_metric(payload: MetricCreate, request: Request):
    if payload.value_numeric is None and not payload.value_text:
        raise HTTPException(400, "Provide value_numeric or value_text")

    recorded = payload.recorded_at or datetime.now(timezone.utc)
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO time_series_metrics
                (subject_id, metric_type, value_numeric, value_text, recorded_at, source, notes)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id, recorded_at
        """,
            uuid.UUID(payload.subject_id),
            payload.metric_type,
            payload.value_numeric,
            payload.value_text,
            recorded,
            payload.source or "manual",
            payload.notes,
        )

    await audit_log("create", get_user_email(request), "time_series_metrics",
                    None, {"metric_type": payload.metric_type, "value": payload.value_numeric})

    return {
        "data": {
            "id": row["id"],
            "subject_id": payload.subject_id,
            "metric_type": payload.metric_type,
            "value_numeric": payload.value_numeric,
            "value_text": payload.value_text,
            "recorded_at": row["recorded_at"].isoformat(),
        }
    }


@router.get("/trend")
async def metric_trend(
    subject_id: str = Query(...),
    metric_type: str = Query(...),
    period: str = Query("weekly"),
):
    try:
        result = await compute_trend(
            subject_id=subject_id, metric_type=metric_type, period=period,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"data": result}


@router.get("/latest")
async def metric_latest(
    subject_id: str = Query(...),
    metric_type: str = Query(...),
):
    result = await latest_metric(subject_id=subject_id, metric_type=metric_type)
    if not result:
        return {"data": None}
    return {"data": result}
