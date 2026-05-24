"""
Tests for the Auto-redesign Phase 10 cross-system integration:

- _action_title_for honors the __vehicle_name hint (pure)
- _vehicle_display_name fallback chain (pure)
- _compute_briefing aggregation + summary line (pure)
- _compute_mileage_nudge heuristic + threshold (pure)
- Integration: briefing endpoint, mileage-nudge endpoint
"""

import json
import os
import urllib.error
import urllib.request
from datetime import date, timedelta

import pytest

from recurrences import _action_title_for
from routers.vehicles import (
    _NUDGE_MIN_UNACCOUNTED_MILES, _compute_briefing,
    _compute_mileage_nudge, _vehicle_display_name,
)


# ─── _action_title_for vehicle-name hint (pure) ─────────────────────────


def test_title_uses_vehicle_name_when_provided():
    data = {"service_type": "Oil change", "__vehicle_name": "Sienna"}
    assert _action_title_for("maintenance_schedule", data, None) == "Sienna: Oil change"


def test_title_falls_back_to_vehicle_prefix_without_hint():
    """Existing behaviour preserved when callers don't supply the hint —
    so non-auto code paths (records.py generic POST) don't break."""
    assert _action_title_for("maintenance_schedule",
                             {"service_type": "Oil change"}, None) == "Vehicle: Oil change"


def test_title_handles_missing_service_type():
    assert (_action_title_for("maintenance_schedule", {"__vehicle_name": "Sienna"}, None)
            == "Sienna: Maintenance")


# ─── _vehicle_display_name (pure) ───────────────────────────────────────


def test_display_name_with_year_make_model():
    assert _vehicle_display_name(
        {"year": 2022, "make": "Toyota", "model": "Sienna"}
    ) == "2022 Toyota Sienna"


def test_display_name_with_partial_fields():
    assert _vehicle_display_name({"make": "Toyota", "model": "Sienna"}) == "Toyota Sienna"
    assert _vehicle_display_name({"year": 2022, "model": "Sienna"}) == "2022 Sienna"


def test_display_name_fallback_when_no_fields():
    assert _vehicle_display_name({}) == "Vehicle"
    assert _vehicle_display_name({"year": None, "make": None, "model": None}) == "Vehicle"


# ─── _compute_briefing (pure) ───────────────────────────────────────────


TODAY = date(2026, 5, 23)


def _veh(vid, **data):
    return {"id": vid, "data": data}


def test_briefing_empty_input():
    out = _compute_briefing([], [], [], TODAY)
    assert out["has_urgent"] is False
    assert out["items"] == []
    assert out["summary_line"] is None
    assert out["as_of"] == TODAY.isoformat()


def test_briefing_includes_registration_within_window():
    in_window = (TODAY + timedelta(days=10)).isoformat()
    out_of_window = (TODAY + timedelta(days=45)).isoformat()
    out = _compute_briefing([
        _veh("v1", year=2022, make="Toyota", model="Sienna",
             registration_expiration=in_window),
        _veh("v2", year=2003, make="Dodge", model="Dakota",
             registration_expiration=out_of_window),
    ], [], [], TODAY, reg_window_days=30)
    kinds = [i["kind"] for i in out["items"]]
    assert kinds == ["registration"]
    assert "Sienna" in out["items"][0]["text"]


def test_briefing_marks_expired_registration_high_severity():
    expired = (TODAY - timedelta(days=5)).isoformat()
    out = _compute_briefing(
        [_veh("v1", make="Toyota", model="Sienna",
              registration_expiration=expired)],
        [], [], TODAY,
    )
    assert out["items"][0]["severity"] == "high"
    assert "ago" in out["items"][0]["text"]


def test_briefing_marks_today_registration_correctly():
    """0 days is "expires today" — the off-by-one was a real risk."""
    out = _compute_briefing(
        [_veh("v1", make="Toyota", model="Sienna",
              registration_expiration=TODAY.isoformat())],
        [], [], TODAY,
    )
    assert "expires today" in out["items"][0]["text"]


def test_briefing_seven_day_window_bumps_severity():
    soon = (TODAY + timedelta(days=5)).isoformat()
    later = (TODAY + timedelta(days=20)).isoformat()
    out = _compute_briefing([
        _veh("v1", make="A", model="x", registration_expiration=soon),
        _veh("v2", make="B", model="y", registration_expiration=later),
    ], [], [], TODAY)
    sev = {i["vehicle_id"]: i["severity"] for i in out["items"]}
    assert sev["v1"] == "high"      # ≤ 7 days
    assert sev["v2"] == "warning"   # 8-30 days


def test_briefing_aggregates_overdue_actions_per_vehicle():
    """One vehicle with multiple overdue items → one briefing item, count
    reflects the aggregate."""
    actions = [
        {"id": "a1", "title": "x", "metadata": {"vehicle_id": "v1"}},
        {"id": "a2", "title": "x", "metadata": {"vehicle_id": "v1"}},
        {"id": "a3", "title": "x", "metadata": {"vehicle_id": "v2"}},
    ]
    out = _compute_briefing([
        _veh("v1", make="Toyota", model="Sienna"),
        _veh("v2", make="Dodge", model="Dakota"),
    ], actions, [], TODAY)
    counts = {i["vehicle_id"]: i["count"] for i in out["items"]
              if i["kind"] == "overdue_maintenance"}
    assert counts == {"v1": 2, "v2": 1}


def test_briefing_skips_overdue_actions_without_vehicle_id():
    """Actions that pre-date Phase 10 won't have metadata.vehicle_id —
    skip them rather than rendering "Vehicle: overdue" with no link."""
    out = _compute_briefing(
        [_veh("v1", make="x", model="y")],
        [{"id": "a1", "title": "x", "metadata": {}}],
        [], TODAY,
    )
    assert not any(i["kind"] == "overdue_maintenance" for i in out["items"])


def test_briefing_includes_one_item_per_recall():
    recalls = [
        {"vehicle_record_id": "v1", "component": "BRAKES",
         "nhtsa_campaign_number": "23V001"},
        {"vehicle_record_id": "v1", "component": "SEAT BELTS",
         "nhtsa_campaign_number": "24V002"},
    ]
    out = _compute_briefing(
        [_veh("v1", make="Toyota", model="Sienna")],
        [], recalls, TODAY,
    )
    recall_items = [i for i in out["items"] if i["kind"] == "recall"]
    assert len(recall_items) == 2


def test_briefing_summary_line_concatenates_first_items():
    expired = (TODAY - timedelta(days=2)).isoformat()
    out = _compute_briefing(
        [_veh("v1", make="Toyota", model="Sienna",
              registration_expiration=expired)],
        [], [{"vehicle_record_id": "v1", "component": "BRAKES",
              "nhtsa_campaign_number": "23V001"}],
        TODAY,
    )
    assert out["summary_line"].startswith("Auto: ")
    assert "Sienna" in out["summary_line"]


def test_briefing_sorts_high_severity_first():
    """A warning-severity item shouldn't push a high-severity item out
    of the summary's lead."""
    expired = (TODAY - timedelta(days=2)).isoformat()   # high
    expiring = (TODAY + timedelta(days=20)).isoformat()  # warning
    out = _compute_briefing([
        _veh("v1", make="A", model="x", registration_expiration=expiring),
        _veh("v2", make="B", model="y", registration_expiration=expired),
    ], [], [], TODAY)
    assert out["items"][0]["severity"] == "high"


# ─── _compute_mileage_nudge (pure) ──────────────────────────────────────


def test_nudge_below_threshold_does_not_suggest():
    """Phase 10 spec: a small unaccounted gap is likely commute, don't
    nag. The threshold is the contract — locked in here."""
    assert _NUDGE_MIN_UNACCOUNTED_MILES > 0
    out = _compute_mileage_nudge(80, 0, 7)
    assert out["suggest_logging"] is False
    assert out["text"] is None


def test_nudge_above_threshold_suggests_with_text():
    out = _compute_mileage_nudge(350, 50, 7)
    assert out["suggest_logging"] is True
    assert out["unaccounted_miles"] == 300.0
    assert "350" in out["text"] and "300" in out["text"]


def test_nudge_treats_negative_unaccounted_as_zero():
    """Logged miles can briefly exceed odometer delta when a trip is
    logged across a metric-write boundary. Don't go negative."""
    out = _compute_mileage_nudge(50, 80, 7)
    assert out["unaccounted_miles"] == 0
    assert out["suggest_logging"] is False


def test_nudge_includes_inputs_in_response():
    """The frontend renders these counts; lock the shape."""
    out = _compute_mileage_nudge(300, 100, 14)
    assert out["window_days"] == 14
    assert out["odometer_delta_miles"] == 300.0
    assert out["logged_trip_miles"] == 100.0


# ─── Integration ────────────────────────────────────────────────────────


API_BASE = os.environ.get("LIFEOS_TEST_API", "http://localhost:8000").rstrip("/")


def _get(path):
    with urllib.request.urlopen(f"{API_BASE}{path}", timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode())


@pytest.mark.integration
class TestPhase10API:

    @pytest.fixture(scope="class", autouse=True)
    def _require_api(self):
        try:
            with urllib.request.urlopen(f"{API_BASE}/api/health", timeout=5):
                pass
        except (urllib.error.URLError, OSError) as exc:
            pytest.skip(f"LifeOS API not reachable at {API_BASE}: {exc}")

    def test_briefing_endpoint_shape(self):
        status, body = _get("/api/vehicles/briefing")
        assert status == 200
        d = body["data"]
        assert "as_of" in d
        assert isinstance(d["has_urgent"], bool)
        assert isinstance(d["items"], list)

    def test_briefing_summary_line_is_string_or_null(self):
        _, body = _get("/api/vehicles/briefing")
        s = body["data"]["summary_line"]
        assert s is None or isinstance(s, str)

    def test_mileage_nudge_endpoint_rejects_garbage_window(self):
        # Build a temp vehicle to query against.
        req = urllib.request.Request(
            f"{API_BASE}/api/vehicles",
            method="POST",
            data=json.dumps({"year": 2099, "make": "TestMake",
                             "model": "NudgeTest"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            vid = json.load(r)["data"]["id"]
        try:
            req = urllib.request.Request(
                f"{API_BASE}/api/vehicles/{vid}/mileage-nudge?days=500",
                method="GET",
            )
            try:
                with urllib.request.urlopen(req, timeout=10):
                    pytest.fail("Expected 400")
            except urllib.error.HTTPError as e:
                assert e.code == 400
        finally:
            urllib.request.urlopen(urllib.request.Request(
                f"{API_BASE}/api/vehicles/{vid}/archive",
                method="POST",
                data=json.dumps({"new_status": "archived"}).encode(),
                headers={"Content-Type": "application/json"},
            ), timeout=10)
