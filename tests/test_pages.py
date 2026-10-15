"""Web page rendering tests (CBV pages and app shell)."""

from __future__ import annotations

import pytest
from django.urls import reverse


@pytest.mark.django_db
def test_home_page_renders_for_anonymous(client):
    response = client.get(reverse("core:home"))
    assert response.status_code == 200
    body = response.content.decode()
    assert "Sign in" in body
    assert "manifest.webmanifest" in body
    assert "system health" not in body.lower()
    # The default page is a standalone login screen: no navigation menu.
    assert 'id="app-sidebar"' not in body
    assert 'aria-label="Primary"' not in body


@pytest.mark.django_db
def test_home_page_renders_for_authenticated_user(client, user):
    client.force_login(user)
    response = client.get(reverse("core:home"))
    assert response.status_code == 200
    body = response.content.decode()
    assert "Checklist" in body
    assert "manifest.webmanifest" in body
    assert "system health" in body.lower()
    # Signed in: the full navigation menu is shown.
    assert 'id="app-sidebar"' in body and 'aria-label="Primary"' in body


@pytest.mark.django_db
def test_logout_returns_to_the_login_page(client, user):
    client.force_login(user)
    response = client.post(reverse("accounts:logout"))
    assert response.status_code == 302
    assert response["Location"] == reverse("accounts:login")


@pytest.mark.django_db
def test_health_endpoint_reports_ok(client):
    response = client.get(reverse("core:health"))
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["checks"]["database"]["status"] == "ok"


@pytest.mark.django_db
def test_health_widget_renders_fragment(client):
    response = client.get(reverse("core:health-widget"))
    assert response.status_code == 200
    assert "Operational" in response.content.decode()


@pytest.mark.django_db
def test_offline_page_renders(client):
    response = client.get(reverse("core:offline"))
    assert response.status_code == 200
    assert b"You are offline" in response.content


@pytest.mark.django_db
def test_login_page_renders(client):
    response = client.get(reverse("accounts:login"))
    assert response.status_code == 200
    assert b"Sign in" in response.content


@pytest.mark.django_db
def test_profile_requires_authentication(client):
    response = client.get(reverse("accounts:profile"))
    assert response.status_code == 302
    assert reverse("accounts:login") in response["Location"]


@pytest.mark.django_db
def test_profile_renders_for_authenticated_user(authenticated_client, user, membership):
    response = authenticated_client.get(reverse("accounts:profile"))
    assert response.status_code == 200
    body = response.content.decode()
    assert user.email in body
    assert membership.organization.name in body


@pytest.mark.django_db
def test_unknown_page_returns_custom_404(client):
    response = client.get("/this-does-not-exist/")
    assert response.status_code == 404
    assert b"Page not found" in response.content
