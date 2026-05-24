"""
Tests for the Auto-redesign Phase 3 maintenance subsystem:

- Template library (api/data/maintenance_templates.py) — pure
- mpd / predicted_due_date math helpers (routers/vehicles.py) — pure
- Schedule CRUD + apply-template endpoints — integration

The integration block lives in TestMaintenanceAPI and is marked
``integration``; it auto-skips when the API isn't reachable. Run unit-only
with::

    pytest -m "not integration"

…and live with::

    LIFEOS_TEST_API=http://127.0.0.1:8100 pytest tests/test_maintenance.py
"""

import json
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from data.maintenance_templates import (
    TEMPLATES, get_template, list_templates_summary,
)
from routers.vehicles import _mpd_from_metrics, _predicted_due_iso
from schemas import validate_record


# ─── Template library (pure) ────────────────────────────────────────────


def test_list_templates_summary_shape():
    summary = list_templates_summary()
    assert summary, "expected at least one template"
    for entry in summary:
        assert set(entry) == {"key", "label", "description", "schedule_count"}
        assert isinstance(entry["schedule_count"], int) and entry["schedule_count"] > 0
        assert entry["key"] in TEMPLATES


def test_list_templates_summary_counts_match_registry():
    by_key = {e["key"]: e["schedule_count"] for e in list_templates_summary()}
    for key, tpl in TEMPLATES.items():
        assert by_key[key] == len(tpl["schedules"])


def test_get_template_known_keys():
    for key in TEMPLATES:
        tpl = get_template(key)
        assert tpl is not None
        assert "schedules" in tpl and tpl["schedules"]


def test_get_template_unknown_returns_none():
    assert get_template("definitely-not-a-template") is None


@pytest.mark.parametrize("key", list(TEMPLATES.keys()))
def test_every_template_entry_has_an_interval(key):
    """The API rejects schedules with neither interval_miles nor
    interval_months — every template entry must satisfy that contract."""
    for entry in TEMPLATES[key]["schedules"]:
        has_miles = entry.get("interval_miles") is not None
        has_months = entry.get("interval_months") is not None
        assert has_miles or has_months, (
            f"{key}/{entry.get('service_type')!r} has no interval"
        )


@pytest.mark.parametrize("key", list(TEMPLATES.keys()))
def test_every_template_entry_has_service_type(key):
    for entry in TEMPLATES[key]["schedules"]:
        svc = entry.get("service_type")
        assert isinstance(svc, str) and svc.strip(), f"{key} has entry without service_type"


@pytest.mark.parametrize("key", list(TEMPLATES.keys()))
def test_template_entries_have_unique_service_types(key):
    """skip_duplicates matches by service_type — duplicate names inside a
    single template would silently drop entries on first apply."""
    types = [e["service_type"] for e in TEMPLATES[key]["schedules"]]
    assert len(types) == len(set(types)), f"{key} has duplicate service_type entries"


@pytest.mark.parametrize("key", list(TEMPLATES.keys()))
def test_template_entries_pass_schema_validation(key):
    """Each template entry, decorated with a fake vehicle_record_id, must
    validate cleanly against the maintenance_schedule schema — otherwise the
    apply-template endpoint would 500 on perfectly valid input."""
    for entry in TEMPLATES[key]["schedules"]:
        payload = {**entry, "vehicle_record_id": "00000000-0000-0000-0000-000000000000"}
        validate_record("maintenance_schedule", payload)  # raises on failure


def test_registration_renewal_present_in_every_template():
    """Date-only schedule that produces the recurring registration alerts;
    the spec calls this out explicitly. Skipping it in a template would mean
    a user who applies "Minimal" silently loses registration alerts."""
    for key, tpl in TEMPLATES.items():
        types = {e["service_type"] for e in tpl["schedules"]}
        assert "Registration renewal" in types, f"{key} missing Registration renewal"


# ─── mpd helper (pure) ──────────────────────────────────────────────────


def _metric(value, days_ago):
    return {
        "value_numeric": value,
        "recorded_at": datetime.now(timezone.utc) - timedelta(days=days_ago),
    }


def test_mpd_returns_none_with_fewer_than_two_metrics():
    assert _mpd_from_metrics([]) is None
    assert _mpd_from_metrics([_metric(50000, 0)]) is None


def test_mpd_returns_none_when_span_under_seven_days():
    # 200 mi over 6 days — too short to trust.
    metrics = [_metric(50000, 6), _metric(50200, 0)]
    assert _mpd_from_metrics(metrics) is None


def test_mpd_returns_none_when_mileage_did_not_increase():
    # Odometer reading didn't change — possibly a re-log; can't extrapolate.
    metrics = [_metric(50000, 30), _metric(50000, 0)]
    assert _mpd_from_metrics(metrics) is None


def test_mpd_returns_none_when_mileage_went_backward():
    # Data entry error / vehicle swap. Don't project off a negative.
    metrics = [_metric(50000, 30), _metric(49000, 0)]
    assert _mpd_from_metrics(metrics) is None


def test_mpd_computes_average_when_enough_data():
    # 900 miles over 30 days → 30 mi/day.
    metrics = [_metric(50000, 30), _metric(50900, 0)]
    assert _mpd_from_metrics(metrics) == pytest.approx(30.0)


def test_mpd_uses_only_first_and_last_metric():
    """A reading in the middle that's noisy shouldn't perturb the average —
    the helper deliberately uses endpoints only."""
    metrics = [
        _metric(50000, 30),
        _metric(50100, 25),  # slow week
        _metric(50900, 0),
    ]
    assert _mpd_from_metrics(metrics) == pytest.approx(30.0)


# ─── predicted_due_iso helper (pure) ────────────────────────────────────


def test_predicted_due_iso_basic_projection():
    # 1000 miles to go at 50 mi/day → 20 days from today.
    today = date(2026, 5, 1)
    iso = _predicted_due_iso(50000, 51000, 50.0, today)
    assert iso == "2026-05-21"


def test_predicted_due_iso_returns_today_when_already_due():
    today = date(2026, 5, 1)
    # current >= next_due → days_to_go is 0 or negative → today (or earlier).
    assert _predicted_due_iso(51000, 51000, 50.0, today) == "2026-05-01"


def test_predicted_due_iso_projects_into_past_when_overdue():
    # 100 miles past due at 50/day = 2 days ago.
    today = date(2026, 5, 10)
    assert _predicted_due_iso(50100, 50000, 50.0, today) == "2026-05-08"


@pytest.mark.parametrize("ndm,cur,mpd", [
    (None,  50000, 30.0),    # no target → no projection
    (51000, None,  30.0),    # no current reading
    (51000, 50000, None),    # no cadence
    (51000, 50000, 0),       # mpd zero treated as falsy → no projection
])
def test_predicted_due_iso_returns_none_when_inputs_missing(ndm, cur, mpd):
    assert _predicted_due_iso(cur, ndm, mpd, date.today()) is None


# ─── Schedule validation contract (pure) ────────────────────────────────


def test_maintenance_schedule_validates_with_only_miles():
    out = validate_record("maintenance_schedule", {
        "vehicle_record_id": "00000000-0000-0000-0000-000000000000",
        "service_type": "Oil change",
        "interval_miles": 5000,
    })
    assert out["interval_miles"] == 5000
    assert out["interval_months"] is None


def test_maintenance_schedule_validates_with_only_months():
    out = validate_record("maintenance_schedule", {
        "vehicle_record_id": "00000000-0000-0000-0000-000000000000",
        "service_type": "Registration renewal",
        "interval_months": 12,
    })
    assert out["interval_months"] == 12
    assert out["interval_miles"] is None


def test_maintenance_schedule_rejects_garbage_types():
    # The pydantic schema is permissive (extra="allow") but typed; a string
    # in interval_miles should still fail coercion.
    with pytest.raises(ValidationError):
        validate_record("maintenance_schedule", {
            "service_type": "Oil change",
            "interval_miles": "five thousand",
        })


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
class TestMaintenanceAPI:
    """End-to-end checks against a live API. Each test creates and tears
    down its own vehicle to stay independent."""

    @pytest.fixture(scope="class", autouse=True)
    def _require_api(self):
        try:
            with urllib.request.urlopen(f"{API_BASE}/api/health", timeout=5):
                pass
        except (urllib.error.URLError, OSError) as exc:
            pytest.skip(f"LifeOS API not reachable at {API_BASE}: {exc}")

    @pytest.fixture
    def vehicle_id(self):
        """Provision a clean test vehicle; archive it after the test so it
        doesn't pollute the fleet list."""
        status, body = _request("POST", "/api/vehicles", {
            "year": 2099, "make": "TestMake", "model": "TestModel",
            "current_mileage": 50000,
        })
        assert status in (200, 201), f"create failed: {status} {body}"
        vid = body["data"]["id"]
        try:
            yield vid
        finally:
            _request("POST", f"/api/vehicles/{vid}/archive", {"new_status": "archived"})

    # — Templates —

    def test_list_templates_endpoint(self):
        status, body = _request("GET", "/api/maintenance-templates")
        assert status == 200
        keys = {e["key"] for e in body["data"]}
        assert keys == set(TEMPLATES.keys())

    def test_get_template_endpoint(self):
        status, body = _request("GET", "/api/maintenance-templates/minimal")
        assert status == 200
        assert body["data"]["key"] == "minimal"
        assert body["data"]["schedules"]

    def test_get_template_unknown_returns_404(self):
        status, _ = _request("GET", "/api/maintenance-templates/nope")
        assert status == 404

    # — apply-template —

    def test_apply_template_skips_duplicates_by_default(self, vehicle_id):
        path = f"/api/vehicles/{vehicle_id}/schedules/apply-template"
        status, body1 = _request("POST", path, {"template_key": "minimal"})
        assert status == 200
        created1 = body1["data"]["created"]
        assert len(created1) == len(TEMPLATES["minimal"]["schedules"])
        assert body1["data"]["skipped"] == []

        # Second apply with default skip_duplicates=True → nothing new.
        status, body2 = _request("POST", path, {"template_key": "minimal"})
        assert status == 200
        assert body2["data"]["created"] == []
        assert len(body2["data"]["skipped"]) == len(TEMPLATES["minimal"]["schedules"])

    def test_apply_template_can_duplicate_when_requested(self, vehicle_id):
        path = f"/api/vehicles/{vehicle_id}/schedules/apply-template"
        _request("POST", path, {"template_key": "minimal"})
        status, body = _request("POST", path, {
            "template_key": "minimal", "skip_duplicates": False,
        })
        assert status == 200
        assert len(body["data"]["created"]) == len(TEMPLATES["minimal"]["schedules"])

    def test_apply_unknown_template_returns_404(self, vehicle_id):
        status, _ = _request("POST",
            f"/api/vehicles/{vehicle_id}/schedules/apply-template",
            {"template_key": "nope"})
        assert status == 404

    # — Manual schedule CRUD —

    def test_create_schedule_with_only_miles(self, vehicle_id):
        status, body = _request("POST", "/api/maintenance-schedules", {
            "vehicle_record_id": vehicle_id,
            "service_type": "Custom miles-only",
            "interval_miles": 4000,
        })
        assert status in (200, 201), body
        assert body["data"]["data"]["interval_miles"] == 4000

    def test_create_schedule_with_only_months(self, vehicle_id):
        status, body = _request("POST", "/api/maintenance-schedules", {
            "vehicle_record_id": vehicle_id,
            "service_type": "Custom months-only",
            "interval_months": 9,
        })
        assert status in (200, 201), body
        assert body["data"]["data"]["interval_months"] == 9

    def test_create_schedule_with_no_intervals_rejected(self, vehicle_id):
        status, body = _request("POST", "/api/maintenance-schedules", {
            "vehicle_record_id": vehicle_id,
            "service_type": "No intervals",
        })
        assert status == 400
        assert "interval" in (body.get("detail") or body.get("message") or "").lower()

    def test_edit_schedule_recomputes_next_due_mileage(self, vehicle_id):
        # Create a schedule with last_service_mileage set, then update it.
        status, body = _request("POST", "/api/maintenance-schedules", {
            "vehicle_record_id": vehicle_id,
            "service_type": "Recompute test",
            "interval_miles": 5000,
            "last_service_mileage": 40000,
        })
        sid = body["data"]["id"]

        # Bump last_service_mileage; next_due_mileage should follow.
        status, _ = _request("PATCH", f"/api/maintenance-schedules/{sid}", {
            "last_service_mileage": 45000,
        })
        assert status == 200

        # Read back via /api/records/{id}.
        status, body = _request("GET", f"/api/records/{sid}")
        assert status == 200
        assert body["data"]["data"]["next_due_mileage"] == 50000

    def test_delete_schedule_tags_action_items(self, vehicle_id):
        # Log mileage close to a 5000-mi interval so an action fires.
        status, body = _request("POST", "/api/maintenance-schedules", {
            "vehicle_record_id": vehicle_id,
            "service_type": "Delete-tag test",
            "interval_miles": 5000,
            "last_service_mileage": 45000,
        })
        sid = body["data"]["id"]

        # Push mileage past the due window to guarantee an action item.
        _request("POST", f"/api/vehicles/{vehicle_id}/mileage", {"mileage": 50200})

        # Capture the action item ids the schedule produced — after delete
        # the FK is nulled so we can't look them up via source_record_id.
        status, actions = _request("GET",
            "/api/actions?domain=auto&status=pending&per_page=100")
        assert status == 200
        action_ids = {a["id"] for a in actions["data"] if a.get("source_record_id") == sid}
        assert action_ids, "expected at least one action item for the schedule"

        # Delete the schedule.
        status, _ = _request("DELETE", f"/api/maintenance-schedules/{sid}")
        assert status == 200

        # Action items survive (FK was SET NULL) and carry schedule_deleted=true.
        status, actions = _request("GET",
            "/api/actions?domain=auto&status=pending&per_page=100")
        survivors = [a for a in actions["data"] if a["id"] in action_ids]
        assert len(survivors) == len(action_ids), "action items should not be deleted"
        for a in survivors:
            assert a["source_record_id"] is None, "source_record_id should be nulled"
            assert a.get("metadata", {}).get("schedule_deleted") is True
