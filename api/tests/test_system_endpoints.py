"""
Integration smoke tests for the system endpoints.

These run against a live API. Base URL comes from $LIFEOS_TEST_API and
defaults to http://localhost:8000 (the in-container address). The whole
module is skipped if the API is unreachable, so unit-test runs stay green
without a running stack.

    # from the host:
    LIFEOS_TEST_API=http://127.0.0.1:8100 pytest tests/test_system_endpoints.py
"""

import json
import os
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.integration

API_BASE = os.environ.get("LIFEOS_TEST_API", "http://localhost:8000").rstrip("/")


def _get(path):
    """GET a JSON endpoint, returning (status_code, parsed_body)."""
    with urllib.request.urlopen(f"{API_BASE}{path}", timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode())


@pytest.fixture(scope="module", autouse=True)
def _require_api():
    try:
        _get("/api/health")
    except (urllib.error.URLError, OSError) as exc:
        pytest.skip(f"LifeOS API not reachable at {API_BASE}: {exc}")


def test_health_reports_status():
    status, body = _get("/api/health")
    assert status == 200
    assert body["status"] in ("healthy", "degraded")
    assert "database" in body and "qdrant" in body


def test_stats_has_expected_keys():
    _, body = _get("/api/stats")
    data = body["data"]
    for key in ("documents", "storage_bytes", "chunks", "subjects", "by_domain"):
        assert key in data


def test_domains_includes_tax():
    _, body = _get("/api/domains")
    assert "tax" in body["data"]


def test_system_config_never_leaks_secret_values():
    _, body = _get("/api/system/config")
    data = body["data"]
    assert "features" in data and "groups" in data

    secret_settings = [
        s for g in data["groups"] for s in g["settings"] if s["secret"]
    ]
    assert secret_settings, "expected at least one secret setting (e.g. api keys)"
    for setting in secret_settings:
        # Secrets are reported only as a set/not-set boolean, never the value.
        assert isinstance(setting["value"], bool), (
            f"{setting['key']} leaked a non-boolean value"
        )


def test_system_config_feature_flags_are_boolean():
    _, body = _get("/api/system/config")
    features = body["data"]["features"]
    assert features, "expected feature flags"
    assert all(isinstance(v, bool) for v in features.values())


def test_system_backups_shape():
    _, body = _get("/api/system/backups")
    data = body["data"]
    assert isinstance(data["accessible"], bool)
    assert isinstance(data["backups"], list)
    assert data["count"] == len(data["backups"])
    if data["backups"]:
        first = data["backups"][0]
        assert {"name", "size_bytes", "modified"} <= set(first)
