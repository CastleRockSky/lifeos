"""
trends.py — Reusable trend computation over time_series_metrics.

Compares the current period's average against the prior period of the same
length. Used by the user dashboard and the HealthBot agent API.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from database import get_pool

PERIOD_DAYS = {
    "daily": 1,
    "weekly": 7,
    "monthly": 30,
    "quarterly": 91,
    "yearly": 365,
}


async def compute_trend(
    *,
    subject_id: str,
    metric_type: str,
    period: str = "weekly",
) -> dict:
    """Return current vs prior period averages and direction.

    Result shape:
        {
            "metric_type": "weight",
            "period": "weekly",
            "current_avg": 184.2,
            "prior_avg": 185.8,
            "change": -1.6,
            "pct_change": -0.86,
            "direction": "down" | "up" | "flat",
            "current_count": N,
            "prior_count": M,
        }
    """
    days = PERIOD_DAYS.get(period.lower())
    if days is None:
        raise ValueError(f"Unknown period: {period}")

    sid = uuid.UUID(subject_id)
    now = datetime.now(timezone.utc)
    cur_start = now - timedelta(days=days)
    prior_start = cur_start - timedelta(days=days)

    pool = get_pool()
    async with pool.acquire() as conn:
        cur = await conn.fetchrow("""
            SELECT AVG(value_numeric) AS avg, COUNT(*) AS n
            FROM time_series_metrics
            WHERE subject_id = $1 AND metric_type = $2
              AND value_numeric IS NOT NULL
              AND recorded_at >= $3 AND recorded_at < $4
        """, sid, metric_type, cur_start, now)

        prev = await conn.fetchrow("""
            SELECT AVG(value_numeric) AS avg, COUNT(*) AS n
            FROM time_series_metrics
            WHERE subject_id = $1 AND metric_type = $2
              AND value_numeric IS NOT NULL
              AND recorded_at >= $3 AND recorded_at < $4
        """, sid, metric_type, prior_start, cur_start)

    cur_avg = float(cur["avg"]) if cur["avg"] is not None else None
    prior_avg = float(prev["avg"]) if prev["avg"] is not None else None

    change: Optional[float] = None
    pct_change: Optional[float] = None
    direction = "unknown"
    if cur_avg is not None and prior_avg is not None:
        change = round(cur_avg - prior_avg, 3)
        if prior_avg != 0:
            pct_change = round((change / prior_avg) * 100, 2)
        if abs(change) < (0.01 * max(abs(prior_avg), 1)):
            direction = "flat"
        elif change > 0:
            direction = "up"
        else:
            direction = "down"
    elif cur_avg is not None:
        direction = "no_prior_data"

    return {
        "metric_type": metric_type,
        "period": period,
        "current_avg": round(cur_avg, 3) if cur_avg is not None else None,
        "prior_avg": round(prior_avg, 3) if prior_avg is not None else None,
        "change": change,
        "pct_change": pct_change,
        "direction": direction,
        "current_count": cur["n"],
        "prior_count": prev["n"],
        "current_window": {"start": cur_start.isoformat(), "end": now.isoformat()},
        "prior_window": {"start": prior_start.isoformat(), "end": cur_start.isoformat()},
    }


_RANGE_DAYS = {
    "30d": 30, "90d": 90, "6m": 183, "1y": 365, "all": None,
}


_PERIOD_TRUNC = {
    "daily": "day",
    "weekly": "week",
    "monthly": "month",
}


async def aggregated_series(
    *,
    subject_id: str,
    metric_type: str,
    period: str = "weekly",
    range_key: str = "90d",
    goal: Optional[float] = None,
) -> dict:
    """Return time-bucketed averages with summary stats and projection.

    period: daily | weekly | monthly — date_trunc bucket size.
    range_key: 30d | 90d | 6m | 1y | all.
    goal: optional target value used to project a goal date from the recent slope.
    """
    trunc = _PERIOD_TRUNC.get(period.lower())
    if not trunc:
        raise ValueError(f"Unknown period: {period}")
    if range_key not in _RANGE_DAYS:
        raise ValueError(f"Unknown range: {range_key}")

    sid = uuid.UUID(subject_id)
    pool = get_pool()
    now = datetime.now(timezone.utc)
    window_days = _RANGE_DAYS[range_key]
    since = (now - timedelta(days=window_days)) if window_days else None

    async with pool.acquire() as conn:
        if since is not None:
            rows = await conn.fetch(f"""
                SELECT date_trunc('{trunc}', recorded_at) AS bucket,
                       AVG(value_numeric) AS avg,
                       COUNT(*) AS n
                FROM time_series_metrics
                WHERE subject_id = $1 AND metric_type = $2
                  AND value_numeric IS NOT NULL
                  AND recorded_at >= $3
                GROUP BY bucket
                ORDER BY bucket
            """, sid, metric_type, since)
        else:
            rows = await conn.fetch(f"""
                SELECT date_trunc('{trunc}', recorded_at) AS bucket,
                       AVG(value_numeric) AS avg,
                       COUNT(*) AS n
                FROM time_series_metrics
                WHERE subject_id = $1 AND metric_type = $2
                  AND value_numeric IS NOT NULL
                GROUP BY bucket
                ORDER BY bucket
            """, sid, metric_type)

    points = [
        {"date": r["bucket"].date().isoformat(), "value": float(r["avg"]), "n": r["n"]}
        for r in rows
    ]

    summary: dict = {
        "average": None, "min": None, "max": None,
        "trend_direction": "unknown", "trend_rate": None,
        "goal": goal, "projected_goal_date": None,
    }
    if points:
        values = [p["value"] for p in points]
        summary["average"] = round(sum(values) / len(values), 3)
        summary["min"] = round(min(values), 3)
        summary["max"] = round(max(values), 3)

        # Linear regression slope on the last up-to-12 points (units per day),
        # converted to "units per week" for display.
        recent = points[-12:]
        if len(recent) >= 2:
            d0 = date.fromisoformat(recent[0]["date"]).toordinal()
            xs = [date.fromisoformat(p["date"]).toordinal() - d0 for p in recent]
            ys = [p["value"] for p in recent]
            n = len(xs)
            mean_x = sum(xs) / n
            mean_y = sum(ys) / n
            num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
            den = sum((xs[i] - mean_x) ** 2 for i in range(n))
            slope_per_day = (num / den) if den else 0.0
            slope_per_week = round(slope_per_day * 7, 4)
            summary["trend_rate"] = slope_per_week
            tolerance = max(abs(mean_y), 1) * 0.001
            if abs(slope_per_day) <= tolerance:
                summary["trend_direction"] = "flat"
            elif slope_per_day > 0:
                summary["trend_direction"] = "up"
            else:
                summary["trend_direction"] = "down"

            if goal is not None and slope_per_day != 0:
                current = ys[-1]
                # Days needed to traverse (goal - current) at slope_per_day, only
                # if we're moving toward the goal.
                gap = goal - current
                if (gap > 0 and slope_per_day > 0) or (gap < 0 and slope_per_day < 0):
                    days_to_goal = gap / slope_per_day
                    proj = date.today() + timedelta(days=int(round(days_to_goal)))
                    summary["projected_goal_date"] = proj.isoformat()

    return {
        "data_points": points,
        **summary,
        "metric_type": metric_type,
        "period": period,
        "range": range_key,
    }


async def latest_metric(
    *,
    subject_id: str,
    metric_type: str,
) -> Optional[dict]:
    """Return the most recent value for a metric, or None."""
    sid = uuid.UUID(subject_id)
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT value_numeric, value_text, recorded_at, source, notes
            FROM time_series_metrics
            WHERE subject_id = $1 AND metric_type = $2
            ORDER BY recorded_at DESC
            LIMIT 1
        """, sid, metric_type)
    if not row:
        return None
    return {
        "value_numeric": float(row["value_numeric"]) if row["value_numeric"] is not None else None,
        "value_text": row["value_text"],
        "recorded_at": row["recorded_at"].isoformat(),
        "source": row["source"],
        "notes": row["notes"],
    }
