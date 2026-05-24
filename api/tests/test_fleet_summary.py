"""
Tests for the Auto-redesign Phase 5 fleet-summary subsystem:

- _compute_fleet_summary aggregation math (pure)
- GET /api/vehicles/fleet-summary endpoint (integration)

Run unit-only with ``pytest -m "not integration"``; the integration class
self-skips when the API isn't reachable.
"""

import json
import os
import urllib.error
import urllib.request
from datetime import date, timedelta

import pytest

from routers.vehicles import _compute_fleet_summary, _INACTIVE_STATUSES


TODAY = date(2026, 5, 23)


def _veh(mileage=None, status=None, reg=None):
    out = {}
    if mileage is not None:
        out["current_mileage"] = mileage
    if status is not None:
        out["status"] = status
    if reg is not None:
        out["registration_expiration"] = reg
    return out


def _svc(cost, date_iso):
    return {"cost": cost, "date": date_iso}


# ─── Pure helper tests ──────────────────────────────────────────────────


def test_empty_fleet_returns_zeros():
    out = _compute_fleet_summary([], [], [], TODAY)
    assert out["vehicle_count"] == 0
    assert out["total_fleet_mileage"] == 0
    assert out["ytd_spend"] == 0
    assert out["open_recalls"] == 0
    assert out["registrations_expiring_60d"] == 0
    assert out["overdue_maintenance"] == 0


def test_counts_only_active_vehicles():
    vehicles = [
        _veh(mileage=50000),  # default active
        _veh(mileage=80000, status="active"),
        _veh(mileage=30000, status="archived"),
        _veh(mileage=20000, status="sold"),
        _veh(mileage=10000, status="merged"),
        _veh(mileage=999, status="totaled"),
    ]
    out = _compute_fleet_summary(vehicles, [], [], TODAY)
    # Only the first two should count.
    assert out["vehicle_count"] == 2
    assert out["total_fleet_mileage"] == 130000


def test_inactive_statuses_constant_is_complete():
    """The router constant must include every status the spec considers
    non-working-fleet. Add any new one to both places."""
    assert _INACTIVE_STATUSES == {"merged", "archived", "sold", "totaled"}


def test_total_mileage_skips_vehicles_with_no_reading():
    vehicles = [
        _veh(mileage=50000),
        _veh(),  # never logged mileage
        _veh(mileage=30000),
    ]
    out = _compute_fleet_summary(vehicles, [], [], TODAY)
    assert out["vehicle_count"] == 3
    assert out["total_fleet_mileage"] == 80000


def test_registration_window_inclusive_at_zero_and_sixty():
    vehicles = [
        _veh(reg=TODAY.isoformat()),                              # 0 days → in
        _veh(reg=(TODAY + timedelta(days=60)).isoformat()),       # 60 days → in
        _veh(reg=(TODAY + timedelta(days=61)).isoformat()),       # 61 days → out
        _veh(reg=(TODAY + timedelta(days=-1)).isoformat()),       # already expired → out
        _veh(),                                                   # no reg → out
    ]
    out = _compute_fleet_summary(vehicles, [], [], TODAY)
    assert out["registrations_expiring_60d"] == 2


def test_registration_window_skips_archived_vehicles():
    vehicles = [
        _veh(reg=(TODAY + timedelta(days=5)).isoformat()),
        _veh(reg=(TODAY + timedelta(days=5)).isoformat(), status="archived"),
    ]
    out = _compute_fleet_summary(vehicles, [], [], TODAY)
    # Only the active one counts.
    assert out["registrations_expiring_60d"] == 1


def test_registration_invalid_date_is_ignored():
    vehicles = [_veh(reg="not a date")]
    out = _compute_fleet_summary(vehicles, [], [], TODAY)
    assert out["registrations_expiring_60d"] == 0


def test_ytd_spend_sums_only_current_year():
    services = [
        _svc(100.0, "2024-12-31"),
        _svc(200.0, "2025-01-15"),
        _svc(50.5,  "2026-04-30"),
        _svc(149.5, "2026-05-20"),
    ]
    out = _compute_fleet_summary([], services, [], TODAY)
    assert out["ytd_spend"] == 200.0  # 50.5 + 149.5


def test_ytd_spend_skips_non_numeric_cost():
    services = [
        _svc(100.0, "2026-01-01"),
        {"cost": None, "date": "2026-02-01"},
        {"date": "2026-03-01"},
        _svc(50.0,  "2026-04-01"),
    ]
    out = _compute_fleet_summary([], services, [], TODAY)
    assert out["ytd_spend"] == 150.0


def test_overdue_counts_only_past_due_dates():
    dates = [
        TODAY - timedelta(days=10),     # overdue
        TODAY - timedelta(days=1),      # overdue
        TODAY,                          # not overdue (due_date == today)
        TODAY + timedelta(days=5),      # future
        None,                           # no due date
    ]
    out = _compute_fleet_summary([], [], dates, TODAY)
    assert out["overdue_maintenance"] == 2


def test_open_recalls_is_placeholder_zero_until_phase_7():
    """Phase 5 ships open_recalls=0; Phase 7 will replace this. Lock the
    contract so a regression here is loud."""
    out = _compute_fleet_summary([], [], [], TODAY)
    assert out["open_recalls"] == 0


# ─── Integration: live endpoint ─────────────────────────────────────────


API_BASE = os.environ.get("LIFEOS_TEST_API", "http://localhost:8000").rstrip("/")


def _get(path):
    with urllib.request.urlopen(f"{API_BASE}{path}", timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode())


@pytest.mark.integration
class TestFleetSummaryAPI:

    @pytest.fixture(scope="class", autouse=True)
    def _require_api(self):
        try:
            with urllib.request.urlopen(f"{API_BASE}/api/health", timeout=5):
                pass
        except (urllib.error.URLError, OSError) as exc:
            pytest.skip(f"LifeOS API not reachable at {API_BASE}: {exc}")

    def test_endpoint_returns_expected_keys(self):
        status, body = _get("/api/vehicles/fleet-summary")
        assert status == 200
        d = body["data"]
        assert {
            "vehicle_count", "total_fleet_mileage", "ytd_spend",
            "open_recalls", "registrations_expiring_60d", "overdue_maintenance",
        } <= set(d)

    def test_endpoint_values_are_well_typed(self):
        _, body = _get("/api/vehicles/fleet-summary")
        d = body["data"]
        assert isinstance(d["vehicle_count"], int)
        assert isinstance(d["total_fleet_mileage"], int)
        assert isinstance(d["ytd_spend"], (int, float))
        assert isinstance(d["open_recalls"], int)
        assert isinstance(d["registrations_expiring_60d"], int)
        assert isinstance(d["overdue_maintenance"], int)
