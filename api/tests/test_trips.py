"""
Tests for the Auto-redesign Phase 9 business-mileage subsystem:

- IRS rate lookup (pure) — 2022 mid-year split, missing years, override
  precedence, rate_blob_to_rate coercion
- Report aggregation math (pure) — by_quarter, by_client, missing-year
  handling, deduction math, sort order
- Odometer-vs-miles discrepancy detector (pure)
- Integration tests for trip CRUD, report endpoint, PDF endpoint
"""

import json
import os
import urllib.error
import urllib.request
from datetime import date

import pytest

from data.irs_mileage_rates import (
    DEFAULT_RATES, Rate, known_years, rate_blob_to_rate, rate_for_date,
)
from routers.trips import (
    _check_odometer_discrepancy, _quarter_for, compute_trip_report,
)


VEH_ID = "11111111-2222-3333-4444-555555555555"


# ─── rate_for_date (pure) ───────────────────────────────────────────────


def test_rate_lookup_simple_year():
    assert rate_for_date(date(2024, 6, 15)) == 0.670
    assert rate_for_date(date(2025, 1, 1)) == 0.700


def test_rate_lookup_handles_2022_midyear_split():
    """The Jan→Jun rate and the Jul→Dec rate are different — the lookup
    must pick the right one based on the date, not just the year."""
    assert rate_for_date(date(2022, 1, 15)) == 0.585
    assert rate_for_date(date(2022, 6, 30)) == 0.585
    assert rate_for_date(date(2022, 7, 1)) == 0.625
    assert rate_for_date(date(2022, 12, 31)) == 0.625


def test_rate_lookup_returns_none_for_unknown_year():
    """Future years aren't in DEFAULT_RATES — caller should prompt user
    rather than silently falling back to an arbitrary rate."""
    assert rate_for_date(date(2099, 5, 1)) is None


def test_rate_lookup_override_takes_precedence():
    """User adds 2099 rate via the API — it shows up immediately."""
    overrides = [Rate(2099, 0.85)]
    assert rate_for_date(date(2099, 5, 1), overrides=overrides) == 0.85


def test_rate_lookup_override_can_replace_default():
    """If IRS amends a prior year (rare but happened in 2022), the
    override should win."""
    overrides = [Rate(2024, 0.999)]
    assert rate_for_date(date(2024, 6, 15), overrides=overrides) == 0.999


def test_rate_lookup_override_respects_date_window():
    """Override applies only within its window if dates are set."""
    overrides = [Rate(2024, 0.999, date(2024, 1, 1), date(2024, 6, 30))]
    assert rate_for_date(date(2024, 6, 15), overrides=overrides) == 0.999
    # Outside window → falls back to default.
    assert rate_for_date(date(2024, 7, 1), overrides=overrides) == 0.670


def test_known_years_combines_defaults_and_overrides():
    overrides = [Rate(2099, 0.85)]
    years = known_years(overrides)
    assert 2099 in years
    assert 2024 in years
    assert 2022 in years


# ─── rate_blob_to_rate (pure) ───────────────────────────────────────────


def test_rate_blob_basic_coercion():
    r = rate_blob_to_rate({"year": 2026, "rate": 0.72})
    assert r is not None
    assert r.year == 2026 and r.rate == 0.72


def test_rate_blob_parses_iso_dates():
    r = rate_blob_to_rate({"year": 2026, "rate": 0.72,
                           "start_date": "2026-07-01", "end_date": "2026-12-31"})
    assert r.start_date == date(2026, 7, 1)
    assert r.end_date == date(2026, 12, 31)


def test_rate_blob_rejects_missing_essentials():
    assert rate_blob_to_rate({"year": 2026}) is None
    assert rate_blob_to_rate({"rate": 0.7}) is None
    assert rate_blob_to_rate({"year": "not-a-number", "rate": 0.7}) is None


def test_rate_blob_garbage_dates_become_none():
    r = rate_blob_to_rate({"year": 2026, "rate": 0.7, "start_date": "garbage"})
    assert r.start_date is None


# ─── _quarter_for (pure) ────────────────────────────────────────────────


@pytest.mark.parametrize("d,expected", [
    (date(2025, 1, 1), "Q1"),
    (date(2025, 3, 31), "Q1"),
    (date(2025, 4, 1), "Q2"),
    (date(2025, 6, 30), "Q2"),
    (date(2025, 7, 1), "Q3"),
    (date(2025, 9, 30), "Q3"),
    (date(2025, 10, 1), "Q4"),
    (date(2025, 12, 31), "Q4"),
])
def test_quarter_for_each_month_lands_in_right_quarter(d, expected):
    assert _quarter_for(d) == expected


# ─── _check_odometer_discrepancy (pure) ─────────────────────────────────


def test_discrepancy_no_warning_when_aligned():
    # 50 miles entered, 50 miles odometer delta — perfect match.
    assert _check_odometer_discrepancy(10000, 10050, 50) is None


def test_discrepancy_no_warning_within_10_percent():
    # 100-mi odometer range, 95 miles entered → 5% diff → ok.
    assert _check_odometer_discrepancy(10000, 10100, 95) is None


def test_discrepancy_warning_above_10_percent():
    # 100-mi range, 80 miles entered → 20% diff → warn.
    msg = _check_odometer_discrepancy(10000, 10100, 80)
    assert msg is not None
    assert "10%" in msg


def test_discrepancy_silent_when_inputs_missing():
    assert _check_odometer_discrepancy(None, 100, 50) is None
    assert _check_odometer_discrepancy(0, None, 50) is None
    assert _check_odometer_discrepancy(0, 100, None) is None


def test_discrepancy_silent_when_odometer_inverted():
    # end <= start is malformed; let it through silently rather than
    # warning about nonsense.
    assert _check_odometer_discrepancy(10100, 10000, 50) is None


# ─── compute_trip_report (pure) ─────────────────────────────────────────


def _trip(date_iso, miles, *, client=None, purpose="x"):
    out = {"date": date_iso, "miles": miles, "purpose": purpose}
    if client:
        out["client"] = client
    return out


def test_report_empty_input():
    out = compute_trip_report([], [], 2025)
    assert out["trip_count"] == 0
    assert out["total_business_miles"] == 0
    assert out["total_deduction"] == 0
    assert out["by_quarter"] == {}
    assert out["by_client"] == {}
    assert out["missing_rate_years"] == []


def test_report_aggregates_correctly_across_quarters():
    trips = [
        _trip("2025-02-15", 120),
        _trip("2025-05-20", 85.5),
        _trip("2025-08-03", 210),
        _trip("2025-11-12", 42),
    ]
    out = compute_trip_report(trips, [], 2025)
    assert out["trip_count"] == 4
    assert out["total_business_miles"] == 457.5
    # 457.5 mi @ $0.700 = $320.25
    assert out["total_deduction"] == 320.25
    assert out["by_quarter"]["Q1"]["miles"] == 120.0
    assert out["by_quarter"]["Q2"]["miles"] == 85.5
    assert out["by_quarter"]["Q3"]["miles"] == 210.0
    assert out["by_quarter"]["Q4"]["miles"] == 42.0


def test_report_quarterly_sums_match_lifetime():
    """Spec contract: quarter buckets must sum to the lifetime total."""
    trips = [_trip(f"2025-{m:02d}-15", m * 10.0) for m in range(1, 13)]
    out = compute_trip_report(trips, [], 2025)
    q_total = sum(v["miles"] for v in out["by_quarter"].values())
    assert q_total == pytest.approx(out["total_business_miles"])
    q_ded = sum(v["deduction"] for v in out["by_quarter"].values())
    assert q_ded == pytest.approx(out["total_deduction"])


def test_report_by_client_sorted_by_miles_desc():
    """Spec doesn't mandate it explicitly but the UI relies on the heaviest
    client appearing first to surface the most relevant context."""
    trips = [
        _trip("2025-02-15", 50,  client="Small"),
        _trip("2025-03-15", 500, client="Big"),
        _trip("2025-04-15", 200, client="Medium"),
    ]
    out = compute_trip_report(trips, [], 2025)
    assert list(out["by_client"].keys()) == ["Big", "Medium", "Small"]


def test_report_uses_unassigned_for_trips_without_client():
    trips = [_trip("2025-02-15", 50), _trip("2025-03-15", 60, client="Smithco")]
    out = compute_trip_report(trips, [], 2025)
    assert out["by_client"]["Unassigned"]["miles"] == 50.0
    assert out["by_client"]["Smithco"]["miles"] == 60.0


def test_report_handles_2022_midyear_rates_per_trip():
    """The defining feature of the rate lookup — verify it shows up in the
    report. A Jan trip should use 0.585; a Jul trip should use 0.625."""
    trips = [_trip("2022-03-01", 100), _trip("2022-08-01", 100)]
    out = compute_trip_report(trips, [], 2022)
    # 100 * 0.585 + 100 * 0.625 = 58.5 + 62.5 = 121.0
    assert out["total_deduction"] == 121.0


def test_report_filters_to_tax_year():
    """Trips outside the requested year are ignored."""
    trips = [
        _trip("2024-12-15", 100),
        _trip("2025-01-15", 200),
        _trip("2026-02-15", 50),
    ]
    out = compute_trip_report(trips, [], 2025)
    assert out["trip_count"] == 1
    assert out["total_business_miles"] == 200.0


def test_report_flags_missing_rate_years():
    """Caller (UI) prompts the user to add the rate based on this list."""
    trips = [_trip("2099-05-01", 100)]
    out = compute_trip_report(trips, [], 2099)
    assert out["missing_rate_years"] == [2099]
    assert out["total_deduction"] == 0  # no rate → no deduction
    # Miles still count (the user added them; we won't drop them).
    assert out["total_business_miles"] == 100.0


def test_report_skips_trips_with_non_numeric_miles():
    trips = [
        _trip("2025-02-01", 100),
        {"date": "2025-03-01", "miles": None, "purpose": "x"},
        {"date": "2025-04-01", "purpose": "x"},  # no miles key
    ]
    out = compute_trip_report(trips, [], 2025)
    assert out["trip_count"] == 1
    assert out["total_business_miles"] == 100.0


def test_report_trips_sorted_chronologically():
    trips = [
        _trip("2025-08-15", 1),
        _trip("2025-02-01", 1),
        _trip("2025-12-31", 1),
    ]
    out = compute_trip_report(trips, [], 2025)
    dates = [t["date"] for t in out["trips"]]
    assert dates == ["2025-02-01", "2025-08-15", "2025-12-31"]


# ─── Integration ────────────────────────────────────────────────────────


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
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def _json_request(method, path, body=None):
    status, _, raw = _request(method, path, body)
    try:
        return status, json.loads(raw.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return status, {"raw": raw.decode(errors="replace")}


@pytest.mark.integration
class TestTripsAPI:

    @pytest.fixture(scope="class", autouse=True)
    def _require_api(self):
        try:
            with urllib.request.urlopen(f"{API_BASE}/api/health", timeout=5):
                pass
        except (urllib.error.URLError, OSError) as exc:
            pytest.skip(f"LifeOS API not reachable at {API_BASE}: {exc}")

    @pytest.fixture
    def vehicle_id(self):
        status, body = _json_request("POST", "/api/vehicles", {
            "year": 2099, "make": "TestMake", "model": "TripsTest",
            "current_mileage": 60000,
        })
        vid = body["data"]["id"]
        try:
            yield vid
        finally:
            # Hard-delete any trip rows so they don't skew global reports.
            _, recs = _json_request("GET", f"/api/trips?vehicle_id={vid}&per_page=500")
            for r in recs.get("data", []):
                _json_request("DELETE", f"/api/trips/{r['id']}")
            _json_request("DELETE", f"/api/records/{vid}")

    def test_create_trip_minimal(self, vehicle_id):
        status, body = _json_request("POST", "/api/trips", {
            "vehicle_record_id": vehicle_id,
            "date": "2025-04-15", "miles": 120.0, "purpose": "Client visit",
        })
        assert status in (200, 201), body
        assert body["data"]["data"]["tax_year"] == 2025
        assert body["data"]["data"]["is_round_trip"] is True

    def test_create_trip_with_odometer_discrepancy_warns(self, vehicle_id):
        status, body = _json_request("POST", "/api/trips", {
            "vehicle_record_id": vehicle_id,
            "date": "2025-04-15", "miles": 50, "purpose": "x",
            "start_mileage": 60000, "end_mileage": 60200,
        })
        assert status in (200, 201)
        assert body["data"]["warning"] is not None
        assert "10%" in body["data"]["warning"]

    def test_list_trips_filtered_by_year(self, vehicle_id):
        for d, miles in [("2024-12-15", 50), ("2025-01-15", 100),
                         ("2025-12-15", 75)]:
            _json_request("POST", "/api/trips", {
                "vehicle_record_id": vehicle_id,
                "date": d, "miles": miles, "purpose": "x",
            })
        status, body = _json_request("GET",
            f"/api/trips?vehicle_id={vehicle_id}&tax_year=2025")
        assert status == 200
        assert len(body["data"]) == 2

    def test_report_endpoint_returns_expected_shape(self, vehicle_id):
        _json_request("POST", "/api/trips", {
            "vehicle_record_id": vehicle_id,
            "date": "2025-03-01", "miles": 100, "purpose": "x", "client": "Acme",
        })
        status, body = _json_request("GET",
            f"/api/trips/report?tax_year=2025&vehicle_id={vehicle_id}")
        assert status == 200
        d = body["data"]
        assert d["trip_count"] == 1
        assert d["total_business_miles"] == 100.0
        assert d["total_deduction"] == 70.0  # 100 * 0.700
        assert d["vehicle"]["model"] == "TripsTest"

    def test_pdf_endpoint_returns_pdf(self, vehicle_id):
        _json_request("POST", "/api/trips", {
            "vehicle_record_id": vehicle_id,
            "date": "2025-03-01", "miles": 100, "purpose": "Client visit",
        })
        status, headers, raw = _request("GET",
            f"/api/trips/report.pdf?tax_year=2025&vehicle_id={vehicle_id}")
        assert status == 200
        assert headers.get("content-type") == "application/pdf"
        # PDF magic header.
        assert raw[:4] == b"%PDF"
        # Larger than the minimum PDF stub.
        assert len(raw) > 1000

    def test_irs_rate_upsert_persists_via_endpoint(self):
        # Add a 2099 rate that lets a 2099 report compute deductions.
        status, body = _json_request("POST", "/api/irs-mileage-rates", {
            "year": 2099, "rate": 0.85,
        })
        assert status == 200
        rid = body["data"]["id"]
        try:
            status, body = _json_request("GET", "/api/irs-mileage-rates")
            assert any(o["year"] == 2099 for o in body["data"]["overrides"])
        finally:
            # Clean up so other tests don't see this row.
            _json_request("DELETE", f"/api/records/{rid}")
