"""Security header assertions."""

from __future__ import annotations

import pytest
from django.urls import reverse


@pytest.mark.django_db
def test_csp_header_is_set_and_contains_nonce(client):
    response = client.get(reverse("core:home"))
    csp = response.headers.get("Content-Security-Policy")
    assert csp is not None
    assert "default-src 'self'" in csp
    assert "nonce-" in csp
    assert "object-src 'none'" in csp


@pytest.mark.django_db
def test_csp_has_no_unsafe_eval(client):
    response = client.get(reverse("core:home"))
    csp = response.headers.get("Content-Security-Policy", "")
    assert "'unsafe-eval'" not in csp


@pytest.mark.django_db
def test_baseline_security_headers(client):
    response = client.get(reverse("core:home"))
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "same-origin"


@pytest.mark.django_db
def test_api_response_also_carries_csp(client):
    response = client.get("/api/v1/health/")
    assert "Content-Security-Policy" in response.headers
