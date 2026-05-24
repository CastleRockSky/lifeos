"""
Tests for the Auto-redesign Phase 6 document↔vehicle linker:

- VIN canonicalisation + match logic (pure)
- year+make+model fallback + ambiguity handling (pure)
- GET /api/vehicles/{id}/documents endpoint (integration)
- PATCH /api/documents/{id} linked_record_id flow (integration)

Run unit-only with ``pytest -m "not integration"``.
"""

import json
import os
import urllib.error
import urllib.request

import pytest

from auto_linking import (
    _normalise_vin, _doc_keys, _vehicle_keys, match_document_to_vehicle,
)


# ─── _normalise_vin (pure) ──────────────────────────────────────────────


def test_normalise_vin_uppercases_and_strips():
    assert _normalise_vin("5tdjrkec1ns129572") == "5TDJRKEC1NS129572"
    assert _normalise_vin(" 5TDJRKEC1NS129572 ") == "5TDJRKEC1NS129572"


def test_normalise_vin_strips_internal_whitespace_and_punctuation():
    # Some receipts spell VIN with hyphens or spaces; treat them the same.
    assert _normalise_vin("5TDJ-RKEC-1NS1-29572") == "5TDJRKEC1NS129572"
    assert _normalise_vin("5TDJ RKEC 1NS1 29572") == "5TDJRKEC1NS129572"


def test_normalise_vin_rejects_wrong_length():
    assert _normalise_vin("TOO-SHORT") is None
    assert _normalise_vin("X" * 18) is None


@pytest.mark.parametrize("value", [None, "", 12345, []])
def test_normalise_vin_rejects_non_strings(value):
    assert _normalise_vin(value) is None


# ─── _doc_keys / _vehicle_keys (pure) ───────────────────────────────────


def test_doc_keys_handles_string_and_int_year():
    assert _doc_keys({"vehicle_year": "2022"})["year"] == 2022
    assert _doc_keys({"vehicle_year": 2022})["year"] == 2022
    assert _doc_keys({"vehicle_year": "garbage"})["year"] is None


def test_doc_keys_accepts_alternate_vin_field():
    """If the AI prompt evolves and starts using vehicle_vin, the linker
    should still find it without a code change."""
    keys = _doc_keys({"vehicle_vin": "5TDJRKEC1NS129572"})
    assert keys["vin"] == "5TDJRKEC1NS129572"


def test_doc_keys_lowercases_make_and_model():
    keys = _doc_keys({"vehicle_make": "Toyota", "vehicle_model": "Sienna"})
    assert keys["make"] == "toyota"
    assert keys["model"] == "sienna"


def test_vehicle_keys_mirror_doc_keys():
    """Same canonicalisation must apply to both sides — otherwise matches
    would fail for case/whitespace reasons even with valid data."""
    vk = _vehicle_keys({"vin": "5tdjrkec1ns129572", "year": 2022,
                        "make": " Toyota ", "model": "Sienna"})
    dk = _doc_keys({"vin": "5TDJRKEC1NS129572", "vehicle_year": "2022",
                    "vehicle_make": "toyota", "vehicle_model": "Sienna "})
    assert vk == dk


# ─── match_document_to_vehicle (pure) ───────────────────────────────────


_SIENNA = {"id": "veh-sienna",
           "data": {"vin": "5TDJRKEC1NS129572", "year": 2022,
                    "make": "Toyota", "model": "Sienna"}}
_DAKOTA = {"id": "veh-dakota",
           "data": {"vin": "1B7HG13Z32S700001", "year": 2003,
                    "make": "Dodge", "model": "Dakota"}}


def test_match_returns_none_for_empty_inputs():
    assert match_document_to_vehicle({}, [_SIENNA]) is None
    assert match_document_to_vehicle({"vin": "X"}, []) is None


def test_match_vin_takes_precedence():
    extracted = {"vin": "5tdjrkec1ns129572",
                 "vehicle_year": 2003, "vehicle_make": "Dodge",
                 "vehicle_model": "Dakota"}
    # VIN matches Sienna; year+make+model would match Dakota. VIN wins.
    assert match_document_to_vehicle(extracted, [_SIENNA, _DAKOTA]) == "veh-sienna"


def test_match_falls_back_to_year_make_model():
    extracted = {"vehicle_year": "2003", "vehicle_make": "dodge",
                 "vehicle_model": "Dakota"}
    assert match_document_to_vehicle(extracted, [_SIENNA, _DAKOTA]) == "veh-dakota"


def test_match_returns_none_on_ambiguous_year_make_model():
    """Two vehicles share year+make+model (the live dataset has this — 2
    Dakotas, 3 Siennas pre-merge). The matcher must NOT guess one
    arbitrarily; the caller can flag for review instead."""
    dak2 = {"id": "veh-dakota-2",
            "data": {"vin": None, "year": 2003, "make": "Dodge", "model": "Dakota"}}
    extracted = {"vehicle_year": 2003, "vehicle_make": "Dodge", "vehicle_model": "Dakota"}
    assert match_document_to_vehicle(extracted, [_DAKOTA, dak2]) is None


def test_match_skips_year_make_model_when_any_field_missing():
    """If the doc only has year and make (no model), don't match anyone
    even if a single vehicle happens to be the same year/make."""
    extracted = {"vehicle_year": 2022, "vehicle_make": "Toyota"}
    assert match_document_to_vehicle(extracted, [_SIENNA]) is None


def test_match_ignores_invalid_vin_and_uses_fallback():
    """A garbage VIN string shouldn't poison the y/m/m fallback."""
    extracted = {"vin": "NOT-A-REAL-VIN",
                 "vehicle_year": 2003, "vehicle_make": "Dodge",
                 "vehicle_model": "Dakota"}
    assert match_document_to_vehicle(extracted, [_SIENNA, _DAKOTA]) == "veh-dakota"


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
class TestVehicleDocumentsAPI:

    @pytest.fixture(scope="class", autouse=True)
    def _require_api(self):
        try:
            with urllib.request.urlopen(f"{API_BASE}/api/health", timeout=5):
                pass
        except (urllib.error.URLError, OSError) as exc:
            pytest.skip(f"LifeOS API not reachable at {API_BASE}: {exc}")

    def test_endpoint_returns_empty_dict_for_vehicle_with_no_docs(self):
        # Create a vehicle, query its documents — should be empty.
        status, body = _request("POST", "/api/vehicles", {
            "year": 2099, "make": "TestMake", "model": "DocsEmpty",
        })
        vid = body["data"]["id"]
        try:
            status, body = _request("GET", f"/api/vehicles/{vid}/documents")
            assert status == 200
            assert body["data"] == {}
            assert body["meta"]["total"] == 0
        finally:
            _request("POST", f"/api/vehicles/{vid}/archive", {"new_status": "archived"})

    def test_patch_linked_record_id_round_trips_via_documents_endpoint(self):
        # Need an existing auto document. Skip if the live DB has none — the
        # test isn't useful without one and the env should self-skip.
        status, body = _request("GET", "/api/documents?domain=auto&per_page=1")
        if status != 200 or not body.get("data"):
            pytest.skip("No auto-domain documents in the live DB to link.")
        doc_id = body["data"][0]["id"]
        original_link = body["data"][0].get("linked_record_id")

        # Create a vehicle to link to.
        _, vbody = _request("POST", "/api/vehicles", {
            "year": 2099, "make": "TestMake", "model": "DocsLink",
        })
        vid = vbody["data"]["id"]
        try:
            # Link.
            status, _ = _request("PATCH", f"/api/documents/{doc_id}",
                                 {"linked_record_id": vid})
            assert status == 200
            # Confirm via the per-vehicle endpoint.
            status, vdocs = _request("GET", f"/api/vehicles/{vid}/documents")
            assert status == 200
            all_ids = [d["id"] for lst in vdocs["data"].values() for d in lst]
            assert doc_id in all_ids
            # Unlink (restore original).
            status, _ = _request("PATCH", f"/api/documents/{doc_id}",
                                 {"linked_record_id": original_link})
            assert status == 200
        finally:
            _request("POST", f"/api/vehicles/{vid}/archive", {"new_status": "archived"})

    def test_patch_rejects_nonexistent_linked_record(self):
        status, body = _request("GET", "/api/documents?domain=auto&per_page=1")
        if status != 200 or not body.get("data"):
            pytest.skip("No auto-domain documents in the live DB to test against.")
        doc_id = body["data"][0]["id"]
        status, body = _request("PATCH", f"/api/documents/{doc_id}",
                                {"linked_record_id": "00000000-0000-0000-0000-000000000000"})
        assert status == 400
        assert "not found" in (body.get("detail") or "").lower()
