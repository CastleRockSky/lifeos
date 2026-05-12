"""
routers/trends.py — Trend visualisation API + monthly/quarterly reports (Phase 9).
"""

import uuid
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from database import get_pool
from trends import aggregated_series

router = APIRouter(prefix="/api", tags=["trends"])


@router.get("/trends/{subject_id}/{metric_type}")
async def metric_trend_series(
    subject_id: str,
    metric_type: str,
    period: str = Query("weekly", regex="^(daily|weekly|monthly)$"),
    range: str = Query("90d", regex="^(30d|90d|6m|1y|all)$"),
    goal: Optional[float] = Query(None),
):
    try:
        uuid.UUID(subject_id)
    except ValueError:
        raise HTTPException(400, "Invalid subject id")
    try:
        result = await aggregated_series(
            subject_id=subject_id, metric_type=metric_type,
            period=period, range_key=range, goal=goal,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"data": result}


# ── Monthly / quarterly reports ─────────────────────────────────────────

def _month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end_day = monthrange(year, month)[1]
    end = datetime(year, month, end_day, 23, 59, 59, tzinfo=timezone.utc)
    return start, end


async def _build_report(start: datetime, end: datetime) -> dict:
    pool = get_pool()
    async with pool.acquire() as conn:
        docs_row = await conn.fetchrow("""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE review_status = 'needs_review') AS needs_review
            FROM documents
            WHERE deleted_at IS NULL
              AND ingested_at >= $1 AND ingested_at <= $2
        """, start, end)

        by_domain = await conn.fetch("""
            SELECT domain, COUNT(*) AS n
            FROM documents
            WHERE deleted_at IS NULL
              AND ingested_at >= $1 AND ingested_at <= $2
              AND domain IS NOT NULL
            GROUP BY domain ORDER BY n DESC
        """, start, end)

        actions_created = await conn.fetchval("""
            SELECT COUNT(*) FROM action_items
            WHERE deleted_at IS NULL
              AND created_at >= $1 AND created_at <= $2
        """, start, end)
        actions_completed = await conn.fetchval("""
            SELECT COUNT(*) FROM action_items
            WHERE completed_at IS NOT NULL
              AND completed_at >= $1 AND completed_at <= $2
        """, start, end)
        actions_pending_overdue = await conn.fetchval("""
            SELECT COUNT(*) FROM action_items
            WHERE deleted_at IS NULL AND status = 'pending'
              AND due_date IS NOT NULL AND due_date < $1
        """, end.date())

        metrics_count = await conn.fetchval("""
            SELECT COUNT(*) FROM time_series_metrics
            WHERE recorded_at >= $1 AND recorded_at <= $2
        """, start, end)

        # Per-domain trend snippets (medical weight, financial debt change)
        primary = await conn.fetchrow(
            "SELECT id, name FROM subjects WHERE is_primary = true AND deleted_at IS NULL LIMIT 1"
        )

    extras: dict = {}
    if primary:
        try:
            weight = await aggregated_series(
                subject_id=str(primary["id"]),
                metric_type="weight",
                period="weekly",
                range_key="90d",
            )
            extras["weight"] = weight
        except Exception:
            pass

    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "documents": {
            "total": docs_row["total"],
            "needs_review": docs_row["needs_review"],
            "by_domain": {r["domain"]: r["n"] for r in by_domain},
        },
        "actions": {
            "created": actions_created,
            "completed": actions_completed,
            "pending_overdue": actions_pending_overdue,
        },
        "metrics_recorded": metrics_count,
        "primary_subject": {"id": str(primary["id"]), "name": primary["name"]} if primary else None,
        "trends": extras,
    }


def _format_markdown(report: dict, title: str) -> str:
    docs = report["documents"]
    actions = report["actions"]
    by_domain = docs["by_domain"]
    lines = [f"# {title}", ""]
    lines.append(f"**Window:** {report['window']['start'][:10]} → {report['window']['end'][:10]}")
    lines.append("")
    lines.append("## Documents")
    lines.append(f"- Ingested: **{docs['total']}**")
    lines.append(f"- Needing review: **{docs['needs_review']}**")
    if by_domain:
        lines.append("- By domain:")
        for d, n in by_domain.items():
            lines.append(f"  - {d}: {n}")
    lines.append("")
    lines.append("## Actions")
    lines.append(f"- Created: **{actions['created']}**")
    lines.append(f"- Completed: **{actions['completed']}**")
    lines.append(f"- Currently overdue: **{actions['pending_overdue']}**")
    lines.append("")
    lines.append(f"## Metrics recorded: **{report['metrics_recorded']}**")

    weight = (report.get("trends") or {}).get("weight") or {}
    if weight.get("data_points"):
        lines.append("")
        lines.append("## Weight trend (90d, weekly)")
        avg = weight.get("average")
        direction = weight.get("trend_direction")
        rate = weight.get("trend_rate")
        if avg is not None:
            lines.append(f"- Average: **{avg}**, direction: **{direction}**, rate: **{rate}/wk**")
        for p in weight["data_points"][-10:]:
            lines.append(f"- {p['date']}: {p['value']}")
    return "\n".join(lines)


@router.get("/reports/monthly")
async def monthly_report(
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    format: str = Query("json", regex="^(json|markdown)$"),
):
    start, end = _month_bounds(year, month)
    report = await _build_report(start, end)
    if format == "markdown":
        from fastapi.responses import PlainTextResponse
        title = f"LifeOS — {start.strftime('%B %Y')} report"
        return PlainTextResponse(_format_markdown(report, title))
    return {"data": report}


@router.get("/reports/quarterly")
async def quarterly_report(
    year: int = Query(...),
    quarter: int = Query(..., ge=1, le=4),
    format: str = Query("json", regex="^(json|markdown)$"),
):
    start_month = (quarter - 1) * 3 + 1
    start = datetime(year, start_month, 1, tzinfo=timezone.utc)
    end_month = start_month + 2
    end_day = monthrange(year, end_month)[1]
    end = datetime(year, end_month, end_day, 23, 59, 59, tzinfo=timezone.utc)
    report = await _build_report(start, end)
    if format == "markdown":
        from fastapi.responses import PlainTextResponse
        title = f"LifeOS — Q{quarter} {year} report"
        return PlainTextResponse(_format_markdown(report, title))
    return {"data": report}
