"""
Tests for the Auto-redesign Phase 4 service-record subsystem:

- ServiceRecord schema (pure)
- _compute_cost_summary cost rollup math (pure)
- Service-record CRUD + schedule-linking endpoints (integration)
- Cost-summary endpoint (integration)

Run unit-only with ``pytest -m "not integration"``; the integration class
self-skips when the API isn't reachable.
"""

import json
import os
import urllib.error
import urllib.request
from datetime import date

import pytest

from routers.vehicles import _compute_cost_summary
from schemas import validate_record


# ─── Cost summary math (pure) ───────────────────────────────────────────


def _svc(cost, date_iso, category="preventive", mileage=None):
    out = {"cost": cost, "date": date_iso, "category": category}
    if mileage is not None:
        out["mileage"] = mileage
    return out


def test_cost_summary_empty_returns_zeros():
    out = _compute_cost_summary([], current_mileage=None,
                                mileage_history=[], today=date(2026, 5, 23))
    assert out["lifetime_total"] == 0
    assert out["ytd_total"] == 0
    assert out["by_category"] == {}
    assert out["by_year"] == {}
    assert out["lifetime_per_mile"] is None


def test_cost_summary_sums_lifetime_and_ytd():
    services = [
        _svc(100.0, "2024-06-01"),
        _svc(200.0, "2026-01-15"),
        _svc(50.5,  "2026-04-30"),
    ]
    out = _compute_cost_summary(services, current_mileage=60000,
                                mileage_history=[55000], today=date(2026, 5, 23))
    assert out["lifetime_total"] == 350.50
    # Only the 2026 entries land in YTD.
    assert out["ytd_total"] == 250.50


def test_cost_summary_by_category_sums_per_bucket():
    services = [
        _svc(75.0,  "2026-01-01", "preventive"),
        _svc(660.0, "2026-02-01", "repair"),
        _svc(40.0,  "2026-03-01", "preventive"),
    ]
    out = _compute_cost_summary(services, current_mileage=None,
                                mileage_history=[], today=date(2026, 5, 23))
    assert out["by_category"] == {"preventive": 115.0, "repair": 660.0}
    # Spec contract: category buckets must sum to lifetime_total.
    assert sum(out["by_category"].values()) == pytest.approx(out["lifetime_total"])


def test_cost_summary_by_year_groups_correctly():
    services = [
        _svc(100.0, "2024-04-01"),
        _svc(200.0, "2024-08-01"),
        _svc(60.0,  "2025-01-15"),
        _svc(40.0,  "2026-02-10"),
    ]
    out = _compute_cost_summary(services, current_mileage=None,
                                mileage_history=[], today=date(2026, 5, 23))
    assert out["by_year"] == {"2024": 300.0, "2025": 60.0, "2026": 40.0}


def test_cost_summary_missing_category_falls_back_to_other():
    services = [{"cost": 50.0, "date": "2026-01-01"}]
    out = _compute_cost_summary(services, current_mileage=None,
                                mileage_history=[], today=date(2026, 5, 23))
    assert out["by_category"] == {"other": 50.0}


def test_cost_summary_per_mile_null_when_history_under_1000_miles():
    # 800-mile span — under the 1000-mi guardrail.
    services = [_svc(200.0, "2026-01-01", mileage=49500)]
    out = _compute_cost_summary(services, current_mileage=50300,
                                mileage_history=[49800], today=date(2026, 5, 23))
    assert out["lifetime_per_mile"] is None


def test_cost_summary_per_mile_computes_with_sufficient_history():
    # 5000-mi span, $500 lifetime → 0.10/mile
    services = [_svc(500.0, "2025-01-01", mileage=50000)]
    out = _compute_cost_summary(services, current_mileage=55000,
                                mileage_history=[50000], today=date(2026, 5, 23))
    assert out["lifetime_per_mile"] == pytest.approx(0.1, abs=1e-4)


def test_cost_summary_uses_earliest_mileage_across_services_and_history():
    """The earliest reading might be a metric log, not a service record —
    the per-mile span should reflect that."""
    services = [_svc(400.0, "2026-01-01", mileage=49000)]
    # Earlier mileage from the metrics table.
    out = _compute_cost_summary(services, current_mileage=51000,
                                mileage_history=[45000], today=date(2026, 5, 23))
    # 51000 - 45000 = 6000 mi span; $400 / 6000 ≈ 0.0667
    assert out["lifetime_per_mile"] == pytest.approx(0.0667, abs=1e-4)


def test_cost_summary_skips_non_numeric_cost():
    """Bad/missing cost entries shouldn't crash the rollup or distort totals."""
    services = [
        _svc(50.0, "2026-01-01"),
        {"cost": None, "date": "2026-02-01", "category": "repair"},
        {"date": "2026-03-01", "category": "tires"},  # no cost key
        _svc(25.0, "2026-04-01"),
    ]
    out = _compute_cost_summary(services, current_mileage=None,
                                mileage_history=[], today=date(2026, 5, 23))
    assert out["lifetime_total"] == 75.0


def test_cost_summary_skips_unparseable_date():
    """A garbage date shouldn't crash; it just doesn't land in YTD or by_year."""
    services = [
        _svc(40.0, "definitely not a date"),
        _svc(60.0, "2026-01-01"),
    ]
    out = _compute_cost_summary(services, current_mileage=None,
                                mileage_history=[], today=date(2026, 5, 23))
    assert out["lifetime_total"] == 100.0
    assert out["ytd_total"] == 60.0
    assert out["by_year"] == {"2026": 60.0}


# ─── ServiceRecord schema (pure) ────────────────────────────────────────


def test_service_record_defaults_category_to_preventive():
    out = validate_record("service_record", {
        "vehicle_record_id": "00000000-0000-0000-0000-000000000000",
        "service_type": "Oil change",
        "date": "2026-05-23",
    })
    assert out["category"] == "preventive"


def test_service_record_accepts_explicit_category():
    out = validate_record("service_record", {
        "vehicle_record_id": "00000000-0000-0000-0000-000000000000",
        "service_type": "Brake job",
        "category": "repair",
    })
    assert out["category"] == "repair"


def test_service_record_parts_round_trips_as_list():
    out = validate_record("service_record", {
        "vehicle_record_id": "00000000-0000-0000-0000-000000000000",
        "service_type": "Oil change",
        "parts": ["oil filter", "5qt 5W30"],
    })
    assert out["parts"] == ["oil filter", "5qt 5W30"]


# ─── Integration: live endpoints ────────────────────────────────────────


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
class TestServiceRecordsAPI:
    """End-to-end checks against a live API. Each test creates and archives
    its own vehicle so it stays independent of fleet state."""

    @pytest.fixture(scope="class", autouse=True)
    def _require_api(self):
        try:
            with urllib.request.urlopen(f"{API_BASE}/api/health", timeout=5):
                pass
        except (urllib.error.URLError, OSError) as exc:
            pytest.skip(f"LifeOS API not reachable at {API_BASE}: {exc}")

    @pytest.fixture
    def vehicle_id(self):
        status, body = _request("POST", "/api/vehicles", {
            "year": 2099, "make": "TestMake", "model": "Phase4",
            "current_mileage": 50000,
        })
        assert status in (200, 201), f"create failed: {status} {body}"
        vid = body["data"]["id"]
        try:
            yield vid
        finally:
            # Hard-delete the dependents so they don't skew global stats
            # (cost-summary, fleet rollups). The vehicle itself is archived.
            _, recs = _request("GET",
                "/api/records?record_type=service_record&per_page=200")
            for r in recs.get("data", []):
                if r["data"].get("vehicle_record_id") == vid:
                    _request("DELETE", f"/api/service-records/{r['id']}")
            _, scheds = _request("GET",
                "/api/records?record_type=maintenance_schedule&per_page=200")
            for s in scheds.get("data", []):
                if s["data"].get("vehicle_record_id") == vid:
                    _request("DELETE", f"/api/maintenance-schedules/{s['id']}")
            _request("POST", f"/api/vehicles/{vid}/archive", {"new_status": "archived"})

    def test_create_service_record_minimal(self, vehicle_id):
        status, body = _request("POST", "/api/service-records", {
            "vehicle_record_id": vehicle_id,
            "date": "2026-05-23",
            "service_type": "Oil change",
        })
        assert status in (200, 201), body
        assert body["data"]["data"]["category"] == "preventive"

    def test_create_service_record_full_fields(self, vehicle_id):
        status, body = _request("POST", "/api/service-records", {
            "vehicle_record_id": vehicle_id,
            "date": "2026-05-23",
            "service_type": "Oil change",
            "category": "preventive",
            "mileage": 50100,
            "provider": "Joe's Garage",
            "cost": 89.99,
            "parts": ["oil filter", "5qt 5W30"],
            "notes": "Smelled funny when I picked it up.",
        })
        assert status in (200, 201), body
        d = body["data"]["data"]
        assert d["parts"] == ["oil filter", "5qt 5W30"]
        assert d["cost"] == 89.99
        assert d["notes"].startswith("Smelled")

    def test_link_to_schedule_bumps_last_service_fields(self, vehicle_id):
        # Create a schedule first
        status, body = _request("POST", "/api/maintenance-schedules", {
            "vehicle_record_id": vehicle_id,
            "service_type": "Oil change",
            "interval_miles": 5000,
            "last_service_mileage": 40000,
        })
        sched_id = body["data"]["id"]

        # Log a service that should update the schedule
        status, _ = _request("POST",
            f"/api/service-records?link_to_schedule_id={sched_id}", {
                "vehicle_record_id": vehicle_id,
                "date": "2026-05-23",
                "service_type": "Oil change",
                "mileage": 50100,
                "cost": 89.99,
            })
        assert status in (200, 201)

        # Schedule should now have last_service_mileage = 50100 and a
        # recomputed next_due_mileage = 55100.
        status, body = _request("GET", f"/api/records/{sched_id}")
        d = body["data"]["data"]
        assert d["last_service_mileage"] == 50100
        assert d["last_service_date"] == "2026-05-23"
        assert d["next_due_mileage"] == 55100

    def test_patch_service_record(self, vehicle_id):
        status, body = _request("POST", "/api/service-records", {
            "vehicle_record_id": vehicle_id,
            "date": "2026-05-23",
            "service_type": "Oil change",
            "cost": 50.0,
        })
        sid = body["data"]["id"]

        status, _ = _request("PATCH", f"/api/service-records/{sid}",
                             {"cost": 75.0, "category": "repair"})
        assert status == 200

        status, body = _request("GET", f"/api/records/{sid}")
        assert body["data"]["data"]["cost"] == 75.0
        assert body["data"]["data"]["category"] == "repair"

    def test_delete_service_record(self, vehicle_id):
        status, body = _request("POST", "/api/service-records", {
            "vehicle_record_id": vehicle_id,
            "date": "2026-05-23",
            "service_type": "Oil change",
        })
        sid = body["data"]["id"]
        status, _ = _request("DELETE", f"/api/service-records/{sid}")
        assert status == 200
        # Subsequent GET returns 404.
        status, _ = _request("GET", f"/api/records/{sid}")
        assert status == 404

    def test_cost_summary_endpoint(self, vehicle_id):
        # Log a few services so the rollup has something to work with.
        for date_iso, cat, cost, mileage in [
            ("2026-01-15", "preventive", 75.0,  50050),
            ("2026-03-10", "repair",     250.0, 50800),
            ("2025-08-01", "tires",      650.0, 49000),
        ]:
            _request("POST", "/api/service-records", {
                "vehicle_record_id": vehicle_id,
                "date": date_iso, "service_type": "x", "category": cat,
                "cost": cost, "mileage": mileage,
            })

        status, body = _request("GET", f"/api/vehicles/{vehicle_id}/cost-summary")
        assert status == 200
        d = body["data"]
        assert d["lifetime_total"] == 975.0
        assert d["ytd_total"] == 325.0  # 2026 entries only
        assert d["by_category"]["preventive"] == 75.0
        assert d["by_category"]["repair"] == 250.0
        assert d["by_category"]["tires"] == 650.0
        # Vehicle has no mileage log, current_mileage=50000, earliest service
        # mileage = 49000 → 1000-mi span (right at the boundary).
        # The guardrail is >= 1000, so per-mile should compute.
        assert d["lifetime_per_mile"] is not None
