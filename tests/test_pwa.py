"""PWA foundation: manifest and service worker."""

from __future__ import annotations

import json

import pytest
from django.conf import settings
from django.urls import reverse


@pytest.mark.django_db
def test_manifest_is_valid_json(client):
    response = client.get(reverse("core:manifest"))
    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/manifest+json")
    data = json.loads(response.content)
    assert data["name"]
    assert data["start_url"] == "/"
    assert data["display"] == "standalone"
    assert len(data["icons"]) == 3
    purposes = {icon["purpose"] for icon in data["icons"]}
    assert "maskable" in purposes


@pytest.mark.django_db
def test_service_worker_is_served_from_root(client):
    response = client.get(reverse("core:service-worker"))
    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/javascript")
    assert response.headers["Service-Worker-Allowed"] == "/"
    assert "CACHE_VERSION" in response.content.decode()


@pytest.mark.django_db
def test_manifest_icons_exist_on_disk():
    static_root = settings.BASE_DIR / "static"
    for name in (
        "icon-192.png",
        "icon-512.png",
        "icon-maskable-512.png",
        "apple-touch-icon.png",
        "favicon.ico",
    ):
        assert (static_root / "icons" / name).is_file(), f"missing icon: {name}"
