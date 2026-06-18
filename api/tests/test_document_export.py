"""
Tests for per-vehicle document zip export:

- _zip_component path-component sanitiser (pure)
- GET /api/vehicles/{id}/documents.zip endpoint (integration)

Run unit-only with ``pytest -m "not integration"``; the integration class
self-skips when the API isn't reachable.
"""

import io
import json
import os
import urllib.error
import urllib.request
import zipfile

import pytest

from routers.vehicles import _zip_component


# ─── _zip_component (pure) ──────────────────────────────────────────────


def test_zip_component_strips_path_separators():
    # A component must never carry a path separator into the archive (which
    # would create nested dirs or enable traversal). Separators become '_'.
    for raw in ["../../etc/passwd", "a/b\\c", "/etc/shadow", "x\\y"]:
        out = _zip_component(raw)
        assert "/" not in out and "\\" not in out
    assert _zip_component("a/b\\c") == "a_b_c"


def test_zip_component_strips_surrounding_dots_and_spaces():
    assert _zip_component("  Service Receipt . ") == "Service Receipt"
    assert _zip_component("...hidden") == "hidden"


def test_zip_component_drops_control_chars():
    assert _zip_component("re\x00port\t.pdf") == "report.pdf"


def test_zip_component_empty_falls_back():
    assert _zip_component("") == "file"
    assert _zip_component(None) == "file"
    assert _zip_component("   ") == "file"
    assert _zip_component(". .") == "file"


def test_zip_component_keeps_normal_names():
    assert _zip_component("20260617_103952.pdf") == "20260617_103952.pdf"
    assert _zip_component("Sequoia_CKP_Harness_Repair_Record.pdf") == \
        "Sequoia_CKP_Harness_Repair_Record.pdf"


# ─── Integration: live endpoint ─────────────────────────────────────────


API_BASE = os.environ.get("LIFEOS_TEST_API", "http://localhost:8000").rstrip("/")


def _request(method, path, body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        method=method,
        data=data,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = resp.read()
            return resp.status, payload if raw else json.loads(payload)
    except urllib.error.HTTPError as e:
        return e.code, None


@pytest.mark.integration
class TestVehicleDocumentZip:

    @pytest.fixture(scope="class", autouse=True)
    def _require_api(self):
        try:
            with urllib.request.urlopen(f"{API_BASE}/api/health", timeout=5):
                pass
        except (urllib.error.URLError, OSError) as exc:
            pytest.skip(f"LifeOS API not reachable at {API_BASE}: {exc}")

    def test_invalid_id_returns_400(self):
        status, _ = _request("GET", "/api/vehicles/not-a-uuid/documents.zip", raw=True)
        assert status == 400

    def test_empty_vehicle_returns_404(self):
        # A freshly created vehicle has no linked documents → 404, not an
        # empty/corrupt zip.
        _, body = _request("POST", "/api/vehicles", {
            "year": 2099, "make": "TestMake", "model": "ZipEmpty",
        })
        vid = body["data"]["id"]
        try:
            status, _ = _request("GET", f"/api/vehicles/{vid}/documents.zip", raw=True)
            assert status == 404
        finally:
            _request("DELETE", f"/api/records/{vid}")

    def test_zip_is_valid_when_docs_exist(self):
        # Find a vehicle that actually has linked documents via the existing
        # per-vehicle documents endpoint; skip if the live DB has none.
        status, fleet = _request("GET", "/api/vehicles/fleet-summary")
        if status != 200:
            pytest.skip("fleet-summary unavailable")
        # Probe documents for any auto document's linked vehicle.
        status, docs = _request("GET", "/api/documents?domain=auto&per_page=25")
        if status != 200 or not docs.get("data"):
            pytest.skip("no auto documents in live DB")
        vid = next((d.get("linked_record_id") for d in docs["data"]
                    if d.get("linked_record_id")), None)
        if not vid:
            pytest.skip("no linked auto documents to export")
        status, payload = _request("GET", f"/api/vehicles/{vid}/documents.zip", raw=True)
        assert status == 200
        zf = zipfile.ZipFile(io.BytesIO(payload))
        assert zf.testzip() is None      # archive integrity
        assert len(zf.namelist()) >= 1   # at least one file bundled
