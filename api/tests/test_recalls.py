"""
Tests for the Auto-redesign Phase 7 recall subsystem:

- NHTSA response parser (pure) — handles capital + lowercase Results key,
  missing fields, both date formats, dedupe via filter_new_recalls
- VehicleRecall schema validates expected shape
- _compute_fleet_summary now respects open_recalls parameter
- GET/POST recalls endpoints (integration; refresh test is network-bound
  and skipped unless explicitly enabled)
"""

import json
import os
import urllib.error
import urllib.request
from datetime import date

import pytest

from nhtsa import _parse_nhtsa_date, filter_new_recalls, parse_nhtsa_results
from routers.vehicles import _compute_fleet_summary
from schemas import validate_record


VEH_ID = "11111111-2222-3333-4444-555555555555"


# ─── parse_nhtsa_date (pure) ────────────────────────────────────────────


def test_parse_iso_date():
    assert _parse_nhtsa_date("2024-08-15") == date(2024, 8, 15)


def test_parse_us_date_format():
    # NHTSA's actual format is MM/DD/YYYY.
    assert _parse_nhtsa_date("08/15/2024") == date(2024, 8, 15)


def test_parse_iso_datetime_takes_only_date_part():
    """Some entries arrive with a trailing time component — strip it."""
    assert _parse_nhtsa_date("2024-08-15T00:00:00Z") == date(2024, 8, 15)


@pytest.mark.parametrize("value", [None, "", "garbage", "13/13/13", 12345])
def test_parse_date_garbage_returns_none(value):
    assert _parse_nhtsa_date(value) is None


# ─── parse_nhtsa_results (pure) ─────────────────────────────────────────


def test_parse_results_handles_lowercase_results_key():
    """Live NHTSA returns ``results`` (lowercase); historical docs showed
    ``Results``. Both must work."""
    payload = {"results": [{
        "NHTSACampaignNumber": "21V889000",
        "Component": "SEAT BELTS:REAR",
        "Summary": "Belt anchor may detach",
        "Consequence": "Increased risk of injury",
        "Remedy": "Dealer will inspect and repair",
        "ReportReceivedDate": "11/12/2021",
    }]}
    out = parse_nhtsa_results(payload, VEH_ID)
    assert len(out) == 1
    r = out[0]
    assert r["nhtsa_campaign_number"] == "21V889000"
    assert r["component"] == "SEAT BELTS:REAR"
    assert r["report_received_date"] == "2021-11-12"
    assert r["status"] == "open"
    assert r["vehicle_record_id"] == VEH_ID
    assert r["discovered_at"]  # set by parser, non-empty


def test_parse_results_handles_capital_results_key():
    payload = {"Results": [{
        "NHTSACampaignNumber": "22V001000",
        "Component": "BRAKES",
        "Summary": "x", "Consequence": "y", "Remedy": "z",
    }]}
    out = parse_nhtsa_results(payload, VEH_ID)
    assert len(out) == 1
    assert out[0]["nhtsa_campaign_number"] == "22V001000"


def test_parse_results_returns_empty_for_no_results():
    assert parse_nhtsa_results({"results": []}, VEH_ID) == []
    assert parse_nhtsa_results({}, VEH_ID) == []
    assert parse_nhtsa_results({"Count": 0, "results": None}, VEH_ID) == []


def test_parse_results_skips_entries_without_campaign_number():
    """No campaign number = no way to dedupe → skip silently rather than
    re-importing on every refresh."""
    payload = {"results": [
        {"Component": "missing campaign field"},
        {"NHTSACampaignNumber": "", "Component": "blank campaign"},
        {"NHTSACampaignNumber": "23V100000", "Component": "ok"},
    ]}
    out = parse_nhtsa_results(payload, VEH_ID)
    assert len(out) == 1
    assert out[0]["nhtsa_campaign_number"] == "23V100000"


def test_parse_results_strips_whitespace():
    payload = {"results": [{
        "NHTSACampaignNumber": "  24V001000  ",
        "Component": "  ENGINE  ",
        "Summary": "  oil leak  ",
    }]}
    out = parse_nhtsa_results(payload, VEH_ID)
    assert out[0]["nhtsa_campaign_number"] == "24V001000"
    assert out[0]["component"] == "ENGINE"
    assert out[0]["summary"] == "oil leak"


def test_parse_results_empty_strings_become_none():
    payload = {"results": [{
        "NHTSACampaignNumber": "24V002000",
        "Component": "",
        "Summary": "   ",
        "ReportReceivedDate": "",
    }]}
    out = parse_nhtsa_results(payload, VEH_ID)
    r = out[0]
    assert r["component"] is None
    assert r["summary"] is None
    assert r["report_received_date"] is None


def test_parse_results_tolerates_garbage_payload():
    # Defensive: don't crash if NHTSA returns something unexpected.
    assert parse_nhtsa_results("not a dict", VEH_ID) == []
    assert parse_nhtsa_results(None, VEH_ID) == []


def test_parse_results_skips_non_dict_entries():
    payload = {"results": [None, "string", {"NHTSACampaignNumber": "OK"}]}
    out = parse_nhtsa_results(payload, VEH_ID)
    assert len(out) == 1


# ─── filter_new_recalls (pure) ──────────────────────────────────────────


def test_filter_new_recalls_dedupes_by_campaign_number():
    parsed = [
        {"nhtsa_campaign_number": "21V001000", "component": "A"},
        {"nhtsa_campaign_number": "22V002000", "component": "B"},
        {"nhtsa_campaign_number": "23V003000", "component": "C"},
    ]
    existing = {"22V002000"}
    new = filter_new_recalls(parsed, existing)
    assert [r["nhtsa_campaign_number"] for r in new] == ["21V001000", "23V003000"]


def test_filter_new_recalls_drops_entries_without_campaign():
    """Defensive — parser shouldn't emit these but doublecheck."""
    parsed = [
        {"nhtsa_campaign_number": "21V001000"},
        {"nhtsa_campaign_number": None},
        {"nhtsa_campaign_number": ""},
    ]
    assert len(filter_new_recalls(parsed, set())) == 1


def test_filter_new_recalls_when_all_existing():
    parsed = [{"nhtsa_campaign_number": "X"}, {"nhtsa_campaign_number": "Y"}]
    assert filter_new_recalls(parsed, {"X", "Y"}) == []


# ─── VehicleRecall schema (pure) ────────────────────────────────────────


def test_vehicle_recall_schema_round_trips_parser_output():
    """The parser output should always validate cleanly — if not, the
    refresh endpoint would 422 on perfectly real NHTSA data."""
    payload = {"results": [{
        "NHTSACampaignNumber": "21V889000",
        "Component": "SEAT BELTS",
        "Summary": "x", "Consequence": "y", "Remedy": "z",
        "ReportReceivedDate": "11/12/2021",
    }]}
    parsed = parse_nhtsa_results(payload, VEH_ID)
    for r in parsed:
        out = validate_record("vehicle_recall", r)
        assert out["status"] == "open"
        assert out["nhtsa_campaign_number"] == "21V889000"


def test_vehicle_recall_defaults_status_to_open():
    out = validate_record("vehicle_recall", {
        "vehicle_record_id": VEH_ID,
        "nhtsa_campaign_number": "X",
    })
    assert out["status"] == "open"


# ─── _compute_fleet_summary respects open_recalls (pure) ────────────────


def test_fleet_summary_open_recalls_kwarg_threads_through():
    out = _compute_fleet_summary([{"current_mileage": 50000}], [], [],
                                 date(2026, 5, 23), open_recalls=4)
    assert out["open_recalls"] == 4


def test_fleet_summary_open_recalls_defaults_to_zero():
    """No regression for the Phase 5 callers that didn't pass the new kwarg."""
    out = _compute_fleet_summary([], [], [], date(2026, 5, 23))
    assert out["open_recalls"] == 0


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
class TestRecallsAPI:

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
            "year": 2099, "make": "TestMake", "model": "RecallsTest",
        })
        vid = body["data"]["id"]
        try:
            yield vid
        finally:
            # Hard-delete any recall rows the test created so they don't
            # leak into fleet counters / Coach / etc.
            _, rec = _request("GET", f"/api/vehicles/{vid}/recalls")
            for r in rec.get("data", []):
                _request("DELETE", f"/api/records/{r['id']}")
            _request("DELETE", f"/api/records/{vid}")

    def test_list_recalls_empty_for_new_vehicle(self, vehicle_id):
        status, body = _request("GET", f"/api/vehicles/{vehicle_id}/recalls")
        assert status == 200
        assert body["data"] == []

    def test_acknowledge_and_resolve_lifecycle(self, vehicle_id):
        # Seed a recall by direct POST against the generic records endpoint,
        # avoiding the NHTSA round-trip.
        status, body = _request("POST", "/api/records", {
            "record_type": "vehicle_recall",
            "domain": "auto",
            "data": {
                "vehicle_record_id": vehicle_id,
                "nhtsa_campaign_number": "TEST-001",
                "component": "BRAKES",
                "summary": "Test recall",
                "status": "open",
            },
        })
        assert status in (200, 201), body
        rid = body["data"]["id"]

        # Acknowledge
        status, _ = _request("POST", f"/api/recalls/{rid}/acknowledge")
        assert status == 200
        status, body = _request("GET", f"/api/records/{rid}")
        assert body["data"]["data"]["status"] == "acknowledged"
        assert body["data"]["data"]["acknowledged_at"]

        # Resolve with notes
        status, _ = _request("POST", f"/api/recalls/{rid}/resolve",
                             {"notes": "Fixed at dealer 2026-05-23"})
        assert status == 200
        status, body = _request("GET", f"/api/records/{rid}")
        d = body["data"]["data"]
        assert d["status"] == "resolved"
        assert d["resolved_at"]
        assert d["notes"] == "Fixed at dealer 2026-05-23"

    def test_acknowledge_rejects_already_resolved(self, vehicle_id):
        status, body = _request("POST", "/api/records", {
            "record_type": "vehicle_recall", "domain": "auto",
            "data": {"vehicle_record_id": vehicle_id,
                     "nhtsa_campaign_number": "TEST-002", "status": "resolved"},
        })
        rid = body["data"]["id"]
        status, body = _request("POST", f"/api/recalls/{rid}/acknowledge")
        assert status == 400
        assert "resolved" in (body.get("detail") or "").lower()

    def test_refresh_rejects_vehicle_without_make_model_year(self, vehicle_id):
        # Strip make so the refresh can't proceed.
        _request("PATCH", f"/api/vehicles/{vehicle_id}", {"make": None})
        status, body = _request("POST", f"/api/vehicles/{vehicle_id}/recalls/refresh")
        assert status == 400
        assert "make" in (body.get("detail") or "").lower()
