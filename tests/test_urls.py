"""URL routing / namespace tests."""

from __future__ import annotations

import pytest
from django.urls import reverse


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("core:home", "/"),
        ("core:health", "/health/"),
        ("core:health-widget", "/health/widget/"),
        ("core:manifest", "/manifest.webmanifest"),
        ("core:service-worker", "/service-worker.js"),
        ("core:offline", "/offline/"),
        ("accounts:login", "/auth/login/"),
        ("accounts:logout", "/auth/logout/"),
        ("accounts:profile", "/auth/profile/"),
        ("v1:root", "/api/v1/"),
        ("v1:health", "/api/v1/health/"),
    ],
)
def test_reverse_routes(name, expected):
    assert reverse(name) == expected


def test_admin_is_mounted():
    assert reverse("admin:index") == "/admin/"


def test_api_schema_routes_are_mounted():
    assert reverse("api-schema") == "/api/schema/"
    assert reverse("api-docs") == "/api/docs/"
