"""
Tests for the Auto-redesign Phase 8 mileage-history subsystem:

- _bucket_mileage_points (day/week/month, sparse + dense data, last-write-wins)
- _assess_data_quality (insufficient/limited/good thresholds)
- GET /api/vehicles/{id}/mileage-history endpoint (integration)
"""

import json
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

import pytest

from routers.vehicles import (
    _MIN_DAYS_FOR_TRENDS, _MIN_POINTS_FOR_TRENDS,
    _assess_data_quality, _bucket_mileage_points,
)


def _m(value, days_ago=0, hours_ago=0):
    return {
        "value_numeric": value,
        "recorded_at": (datetime.now(timezone.utc)
                        - timedelta(days=days_ago, hours=hours_ago)),
    }


def _m_on(value, date_obj):
    """Build a metric anchored to a specific date (for bucketing tests)."""
    return {"value_numeric": value,
            "recorded_at": datetime.combine(date_obj, datetime.min.time(),
                                            tzinfo=timezone.utc)}


# ─── _bucket_mileage_points (pure) ──────────────────────────────────────


def test_bucket_empty_returns_empty():
    assert _bucket_mileage_points([], "month") == []


def test_bucket_month_collapses_multiple_per_month():
    metrics = [
        _m_on(50000, date(2026, 1, 5)),
        _m_on(50300, date(2026, 1, 20)),
        _m_on(50500, date(2026, 1, 28)),  # last in Jan wins
        _m_on(51000, date(2026, 2, 10)),
    ]
    out = _bucket_mileage_points(metrics, "month")
    assert len(out) == 2
    assert out[0] == {"date": "2026-01-28", "value": 50500.0}
    assert out[1] == {"date": "2026-02-10", "value": 51000.0}


def test_bucket_week_uses_iso_week():
    """Two readings in the same ISO week should collapse to one; readings
    in adjacent weeks should not."""
    # 2026-05-18 is Mon, 2026-05-22 Fri (same ISO week);
    # 2026-05-25 is the following Monday.
    metrics = [
        _m_on(50000, date(2026, 5, 18)),
        _m_on(50100, date(2026, 5, 22)),
        _m_on(50300, date(2026, 5, 25)),
    ]
    out = _bucket_mileage_points(metrics, "week")
    assert len(out) == 2
    assert out[0]["value"] == 50100.0  # last reading of week 21
    assert out[1]["value"] == 50300.0


def test_bucket_week_isocalendar_handles_year_boundary():
    """A Jan 1 that ISO-calendar puts into the prior year's last week
    shouldn't collide with that prior year's earlier readings."""
    metrics = [
        _m_on(50000, date(2025, 12, 30)),  # ISO 2026-W01
        _m_on(50050, date(2026, 1, 4)),    # ISO 2026-W01 (same!)
        _m_on(50100, date(2026, 1, 5)),    # ISO 2026-W02
    ]
    out = _bucket_mileage_points(metrics, "week")
    # First two collapse into one bucket; the third is a new week.
    assert len(out) == 2


def test_bucket_day_keeps_one_per_calendar_day():
    metrics = [
        _m_on(50000, date(2026, 5, 1)),
        _m_on(50010, date(2026, 5, 1)),  # same day → collapses (last wins)
        _m_on(50050, date(2026, 5, 2)),
    ]
    out = _bucket_mileage_points(metrics, "day")
    assert out == [
        {"date": "2026-05-01", "value": 50010.0},
        {"date": "2026-05-02", "value": 50050.0},
    ]


def test_bucket_preserves_chronological_order():
    """Bucket keys are sorted lexically; year-month and YYYY-MM-DD sort
    correctly that way. Verify the output sequence."""
    metrics = [
        _m_on(50000, date(2025, 11, 15)),
        _m_on(50300, date(2026, 1, 5)),
        _m_on(50500, date(2026, 3, 10)),
    ]
    out = _bucket_mileage_points(metrics, "month")
    assert [p["date"] for p in out] == ["2025-11-15", "2026-01-05", "2026-03-10"]


# ─── _assess_data_quality (pure) ────────────────────────────────────────


def test_quality_insufficient_when_fewer_than_min_points():
    assert _MIN_POINTS_FOR_TRENDS >= 2  # invariant
    metrics = [_m(50000, days_ago=30)] * (_MIN_POINTS_FOR_TRENDS - 1)
    assert _assess_data_quality(metrics) == "insufficient"


def test_quality_insufficient_when_span_under_seven_days():
    # Plenty of points (5) but all within 5 days.
    metrics = [
        _m(50000, days_ago=5),
        _m(50100, days_ago=4),
        _m(50200, days_ago=2),
        _m(50300, days_ago=1),
        _m(50350, days_ago=0),
    ]
    assert _assess_data_quality(metrics) == "insufficient"


def test_quality_limited_for_span_between_seven_and_thirty_days():
    metrics = [
        _m(50000, days_ago=20),
        _m(50500, days_ago=10),
        _m(50800, days_ago=0),
    ]
    assert _assess_data_quality(metrics) == "limited"


def test_quality_good_for_span_thirty_or_more_days():
    metrics = [
        _m(50000, days_ago=60),
        _m(50500, days_ago=30),
        _m(51000, days_ago=0),
    ]
    assert _assess_data_quality(metrics) == "good"


def test_quality_constants_have_sane_values():
    """The /api/vehicles/{id}/mileage-history endpoint relies on these
    constants being tight enough to filter out one-off readings."""
    assert _MIN_POINTS_FOR_TRENDS >= 2
    assert _MIN_DAYS_FOR_TRENDS >= 7


# ─── Integration: live endpoint ─────────────────────────────────────────


API_BASE = os.environ.get("LIFEOS_TEST_API", "http://localhost:8000").rstrip("/")


def _request(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        method=method,
        data=data,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        try:
            return e.code, json.loads(body_bytes.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return e.code, {"raw": body_bytes.decode(errors="replace")}


@pytest.mark.integration
class TestMileageHistoryAPI:

    @pytest.fixture(scope="class", autouse=True)
    def _require_api(self):
        try:
            with urllib.request.urlopen(f"{API_BASE}/api/health", timeout=5):
                pass
        except (urllib.error.URLError, OSError) as exc:
            pytest.skip(f"LifeOS API not reachable at {API_BASE}: {exc}")

    def test_returns_insufficient_for_subjectless_vehicle(self):
        status, body = _request("POST", "/api/vehicles", {
            "year": 2099, "make": "TestMake", "model": "NoSubject",
        })
        vid = body["data"]["id"]
        try:
            status, body = _request("GET", f"/api/vehicles/{vid}/mileage-history")
            assert status == 200
            d = body["data"]
            assert d["data_quality"] == "insufficient"
            assert d["points"] == []
            assert d["miles_per_day_recent"] is None
        finally:
            _request("DELETE", f"/api/records/{vid}")

    def test_rejects_invalid_granularity(self):
        # Need any vehicle; create + tear down.
        _, body = _request("POST", "/api/vehicles",
                           {"year": 2099, "make": "TestMake", "model": "G"})
        vid = body["data"]["id"]
        try:
            status, _ = _request("GET",
                f"/api/vehicles/{vid}/mileage-history?granularity=lightyears")
            assert status == 400
        finally:
            _request("DELETE", f"/api/records/{vid}")

    def test_rejects_invalid_since(self):
        _, body = _request("POST", "/api/vehicles",
                           {"year": 2099, "make": "TestMake", "model": "S"})
        vid = body["data"]["id"]
        try:
            status, _ = _request("GET",
                f"/api/vehicles/{vid}/mileage-history?since=not-a-date")
            assert status == 400
        finally:
            _request("DELETE", f"/api/records/{vid}")

    def test_404_for_unknown_vehicle(self):
        status, _ = _request("GET",
            "/api/vehicles/00000000-0000-0000-0000-000000000000/mileage-history")
        assert status == 404
