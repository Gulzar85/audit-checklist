"""REST API contract tests (envelope + error shape)."""

from __future__ import annotations

import pytest


@pytest.mark.django_db
def test_api_root_uses_success_envelope(client):
    response = client.get("/api/v1/")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload.keys()) == {"data", "meta"}
    assert payload["data"]["version"] == "v1"
    assert payload["meta"] == {}


@pytest.mark.django_db
def test_api_health_reports_ok(client):
    response = client.get("/api/v1/health/")
    assert response.status_code == 200
    payload = response.json()
    assert payload["data"]["status"] == "ok"
    assert payload["data"]["checks"]["database"]["status"] == "ok"


@pytest.mark.django_db
def test_api_error_uses_error_envelope(client):
    response = client.post("/api/v1/")
    assert response.status_code == 405
    payload = response.json()
    assert "error" in payload
    assert payload["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert isinstance(payload["error"]["message"], str)
