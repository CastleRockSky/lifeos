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
    _normalise_vin, _doc_keys, _vehicle_keys, _split_combined_vehicle,
    _vins_match, is_phantom_vehicle, is_vin_tail_mileage,
    match_document_to_vehicle,
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


# ─── _vins_match (pure) ─────────────────────────────────────────────────


def test_vins_match_exact():
    assert _vins_match("5TDJRKEC1NS129572", "5TDJRKEC1NS129572") is True


def test_vins_match_one_char_ocr_flip():
    """The real Dakota case: position-11 S got OCR'd as 8 on a scan, which
    exact matching can't dedupe. A single substitution must still match."""
    assert _vins_match("1D7HG38X83S172745", "1D7HG38X838172745") is True


def test_vins_match_rejects_two_char_difference():
    """Two-character divergence is beyond a single OCR glyph — treat as a
    genuinely different VIN, don't merge them."""
    assert _vins_match("1D7HG38X83S172745", "1D7HG38X888172745") is False


def test_vins_match_rejects_length_mismatch_and_none():
    # Different lengths can't be a substitution; None means no VIN to compare.
    assert _vins_match("ABC", "ABCD") is False
    assert _vins_match(None, "5TDJRKEC1NS129572") is False
    assert _vins_match("5TDJRKEC1NS129572", None) is False
    assert _vins_match(None, None) is False


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


def test_match_fuzzy_vin_attaches_single_candidate():
    """An OCR-flipped VIN on a scan (no exact hit) should still attach the
    doc to the one vehicle within a single character — otherwise the doc
    orphans and a duplicate vehicle tends to get created from it."""
    extracted = {"vin": "1B7HG13Z32S700000"}  # one char off _DAKOTA's VIN
    assert match_document_to_vehicle(extracted, [_SIENNA, _DAKOTA]) == "veh-dakota"


def test_match_fuzzy_vin_refuses_to_guess_when_ambiguous():
    """If two vehicles are each within one character of the doc's VIN, don't
    guess — mirror the y/m/m ambiguity rule and return None."""
    near1 = {"id": "veh-a", "data": {"vin": "1B7HG13Z32S700001"}}
    near2 = {"id": "veh-b", "data": {"vin": "1B7HG13Z32S700009"}}
    extracted = {"vin": "1B7HG13Z32S700000"}  # one char from each
    assert match_document_to_vehicle(extracted, [near1, near2]) is None


def test_match_skips_year_make_model_when_any_field_missing():
    """If the doc only has year and make (no model), don't match anyone
    even if a single vehicle happens to be the same year/make."""
    extracted = {"vehicle_year": 2022, "vehicle_make": "Toyota"}
    assert match_document_to_vehicle(extracted, [_SIENNA]) is None


# ─── _split_combined_vehicle (pure) ─────────────────────────────────────


def test_split_combined_vehicle_basic():
    """Service receipts often return "Toyota Sienna" as one field rather
    than split make/model. The fallback splits on first token = make."""
    assert _split_combined_vehicle("Toyota Sienna") == ("Toyota", "Sienna")


def test_split_combined_vehicle_drops_leading_year():
    assert _split_combined_vehicle("2022 Toyota Sienna") == ("Toyota", "Sienna")
    assert _split_combined_vehicle("2003 Dodge Dakota") == ("Dodge", "Dakota")


def test_split_combined_vehicle_handles_multi_word_models():
    """"Honda Civic Si" — model is everything after the make."""
    assert _split_combined_vehicle("Honda Civic Si") == ("Honda", "Civic Si")


def test_split_combined_vehicle_returns_none_for_unparseable():
    assert _split_combined_vehicle("") == (None, None)
    assert _split_combined_vehicle("Toyota") == (None, None)  # need ≥ 2 tokens
    assert _split_combined_vehicle(None) == (None, None)
    assert _split_combined_vehicle(42) == (None, None)


def test_split_combined_vehicle_ignores_non_year_4digit_first_token():
    """Don't strip a 4-digit token that isn't a plausible year (e.g. a
    fleet number that happened to be four digits)."""
    assert _split_combined_vehicle("1234 Toyota Sienna") == ("1234", "Toyota Sienna")


def test_doc_keys_falls_back_to_combined_vehicle_field():
    """The real-world failure: service receipts return ``"vehicle":
    "Toyota Sienna"`` instead of split keys. _doc_keys must catch it."""
    keys = _doc_keys({"vehicle": "Toyota Sienna"})
    assert keys["make"] == "toyota"
    assert keys["model"] == "sienna"


def test_doc_keys_prefers_split_fields_over_combined():
    """If both are present, split keys win (they're more authoritative)."""
    keys = _doc_keys({
        "vehicle": "Honda Civic",
        "vehicle_make": "Toyota",
        "vehicle_model": "Sienna",
    })
    assert keys["make"] == "toyota"
    assert keys["model"] == "sienna"


def test_doc_keys_uses_combined_only_when_split_is_partial():
    """If only `vehicle_make` exists (missing model), still try the
    combined field for the model half."""
    keys = _doc_keys({"vehicle_make": "Toyota", "vehicle": "Toyota Sienna"})
    assert keys["make"] == "toyota"
    assert keys["model"] == "sienna"


# ─── is_phantom_vehicle (pure) ──────────────────────────────────────────


def test_phantom_vehicle_flags_missing_model():
    """The real Volkswagen case: AI created a vehicle with model=null
    out of a "Dual Registration Type" form field on a Sienna
    registration."""
    proposed = {"year": 2022, "make": "Volkswagen", "model": None,
                "notes": "Dual registration type"}
    reason = is_phantom_vehicle(proposed, [])
    assert reason is not None
    assert "model" in reason.lower()


def test_phantom_vehicle_flags_duplicate_when_no_vin():
    """No VIN + triple match against an existing active vehicle = almost
    certainly a duplicate (e.g. a second pass on the same registration)."""
    proposed = {"year": 2022, "make": "Toyota", "model": "Sienna"}
    existing = [{"id": "veh-1",
                 "data": {"year": 2022, "make": "Toyota", "model": "Sienna",
                          "vin": "5TDJRKEC1NS129572"}}]
    reason = is_phantom_vehicle(proposed, existing)
    assert reason is not None
    assert "veh-1" in reason


def test_phantom_vehicle_allows_legitimate_new_record():
    """Different y/m/m, no collision → safe to create."""
    proposed = {"year": 2003, "make": "Dodge", "model": "Dakota",
                "vin": "1B7HG13Z32S700001"}
    existing = [{"id": "veh-1",
                 "data": {"year": 2022, "make": "Toyota", "model": "Sienna"}}]
    assert is_phantom_vehicle(proposed, existing) is None


def test_phantom_vehicle_flags_same_make_when_vin_does_not_match():
    """Stronger guard (2026-06-18): on the AI path, an extracted vehicle whose
    make matches an existing active vehicle but whose VIN matches nothing is
    treated as a probable OCR-garbled duplicate and skipped — this fleet holds
    one vehicle per make. (Was previously allowed through; that let 2-char-OCR
    VIN dups slip past _vins_match and spawn phantom Sequoias.) Genuine second
    cars of an existing make are added via manual POST /api/vehicles, which
    bypasses this guard."""
    proposed = {"year": 2003, "make": "Toyota", "model": "Sequoia",
                "vin": "5TDBT44A35S165893"}  # 53→35 transposition, Hamming 2
    existing = [{"id": "veh-seq",
                 "data": {"year": 2003, "make": "Toyota", "model": "Sequoia",
                          "vin": "5TDBT44A53S165893"}}]
    reason = is_phantom_vehicle(proposed, existing)
    assert reason is not None
    assert "veh-seq" in reason


def test_phantom_vehicle_flags_garbled_year_no_vin_same_make():
    """The "2273"→2023 OCR case: garbled year defeats year+make+model matching,
    but the make still matches the real vehicle, so it's caught."""
    proposed = {"year": 2023, "make": "Toyota", "model": "Sequoia"}
    existing = [{"id": "veh-seq",
                 "data": {"year": 2003, "make": "Toyota", "model": "Sequoia",
                          "vin": "5TDBT44A53S165893"}}]
    assert is_phantom_vehicle(proposed, existing) is not None


def test_phantom_vehicle_flags_duplicate_on_exact_vin():
    """The core dedup fix: a re-extracted vehicle whose VIN already exists
    on an active record is a duplicate, not a new vehicle. Pre-fix this
    fell through and inserted a fresh row (the 6-Dakota fan-out)."""
    proposed = {"year": 2003, "make": "Dodge", "model": "Dakota",
                "vin": "1D7HG38X83S172745"}
    existing = [{"id": "veh-dakota",
                 "data": {"year": 2003, "make": "Dodge", "model": "Dakota",
                          "vin": "1D7HG38X83S172745"}}]
    reason = is_phantom_vehicle(proposed, existing)
    assert reason is not None
    assert "veh-dakota" in reason
    assert "vin" in reason.lower()


def test_phantom_vehicle_flags_duplicate_on_ocr_flipped_vin():
    """The S→8 OCR'd Dakota VIN must still be recognised as the same
    vehicle — exact matching alone wouldn't catch it and a duplicate would
    be created."""
    proposed = {"year": 2003, "make": "Dodge", "model": "Dakota",
                "vin": "1D7HG38X838172745"}  # position-11 S misread as 8
    existing = [{"id": "veh-dakota",
                 "data": {"year": 2003, "make": "Dodge", "model": "Dakota",
                          "vin": "1D7HG38X83S172745"}}]
    reason = is_phantom_vehicle(proposed, existing)
    assert reason is not None
    assert "veh-dakota" in reason


def test_phantom_vehicle_allows_new_y_m_m_with_no_vin():
    """No VIN but no duplicate either — fine. Manual entries from the UI
    often start without VIN and that's expected."""
    proposed = {"year": 2020, "make": "Subaru", "model": "Outback"}
    existing = [{"id": "veh-1",
                 "data": {"year": 2022, "make": "Toyota", "model": "Sienna"}}]
    assert is_phantom_vehicle(proposed, existing) is None


# ─── is_vin_tail_mileage (pure) ─────────────────────────────────────────


def test_vin_tail_mileage_flags_exact_match():
    """The real-world failure: AI extracted mileage=129572 from a Sienna
    receipt whose VIN ended in ...129572. Must be flagged."""
    assert is_vin_tail_mileage(129572, "5TDJRKEC1NS129572") is True


def test_vin_tail_mileage_flags_lowercase_vin():
    """Normalisation kicks in — lowercase VIN still matches."""
    assert is_vin_tail_mileage(129572, "5tdjrkec1ns129572") is True


def test_vin_tail_mileage_safe_when_tail_has_letters():
    """If the last 6 of a VIN aren't all digits, the comparison can't
    spuriously match an integer mileage."""
    assert is_vin_tail_mileage(129572, "5TDJRKEC1NABC123") is False


def test_vin_tail_mileage_safe_for_different_mileage():
    assert is_vin_tail_mileage(45000, "5TDJRKEC1NS129572") is False


def test_vin_tail_mileage_accepts_string_mileage():
    """AI extractions sometimes return mileage as a string."""
    assert is_vin_tail_mileage("129572", "5TDJRKEC1NS129572") is True


def test_vin_tail_mileage_returns_false_on_missing_inputs():
    assert is_vin_tail_mileage(None, "5TDJRKEC1NS129572") is False
    assert is_vin_tail_mileage(129572, None) is False
    assert is_vin_tail_mileage(None, None) is False


def test_vin_tail_mileage_returns_false_on_non_numeric_mileage():
    """Non-numeric mileage strings ("twelve thousand") shouldn't crash."""
    assert is_vin_tail_mileage("twelve thousand", "5TDJRKEC1NS129572") is False


def test_vin_tail_mileage_safe_when_vin_too_short():
    """Malformed VINs (not 17 chars) get normalised to None."""
    assert is_vin_tail_mileage(123, "ABC123") is False


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
            _request("DELETE", f"/api/records/{vid}")

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
            _request("DELETE", f"/api/records/{vid}")

    def test_patch_rejects_nonexistent_linked_record(self):
        status, body = _request("GET", "/api/documents?domain=auto&per_page=1")
        if status != 200 or not body.get("data"):
            pytest.skip("No auto-domain documents in the live DB to test against.")
        doc_id = body["data"][0]["id"]
        status, body = _request("PATCH", f"/api/documents/{doc_id}",
                                {"linked_record_id": "00000000-0000-0000-0000-000000000000"})
        assert status == 400
        assert "not found" in (body.get("detail") or "").lower()
