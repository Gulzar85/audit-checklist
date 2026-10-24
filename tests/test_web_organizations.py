"""Organization pages: list, detail, create, update, status, branding."""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.core.models import ActivityLog
from apps.organizations import services
from apps.organizations.models import Organization

pytestmark = pytest.mark.django_db


def login(client, user):
    client.force_login(user)
    return client


class TestList:
    def test_requires_login(self, client):
        response = client.get(reverse("organizations:list"))
        assert response.status_code == 302 and "/auth/login/" in response["Location"]

    def test_member_sees_only_their_organization(self, client, admin_a, org_a, org_b):
        body = login(client, admin_a).get(reverse("organizations:list")).content.decode()
        assert "Org A" in body and "Org B" not in body

    def test_staff_see_all_and_get_the_create_button(self, client, staff_user, org_a, org_b):
        body = login(client, staff_user).get(reverse("organizations:list")).content.decode()
        assert "Org A" in body and "Org B" in body and reverse("organizations:create") in body

    def test_org_admin_does_not_get_a_create_button(self, client, admin_a):
        body = login(client, admin_a).get(reverse("organizations:list")).content.decode()
        assert reverse("organizations:create") not in body

    def test_search_filter(self, client, staff_user, org_a, org_b):
        body = login(client, staff_user).get(reverse("organizations:list"), {"q": "org b"}).content.decode()
        assert "Org B" in body and "Org A</a>" not in body

    def test_status_filter(self, client, staff_user, org_a, org_b):
        services.suspend_organization(org_b)
        body = login(client, staff_user).get(reverse("organizations:list"), {"status": "suspended"}).content.decode()
        assert "Org B" in body and "Org A</a>" not in body

    def test_empty_state(self, client, make_user):
        body = login(client, make_user()).get(reverse("organizations:list")).content.decode()
        assert "No organizations found" in body

    def test_pagination(self, client, staff_user):
        for i in range(25):
            Organization.objects.create(name=f"Bulk {i:02d}", slug=f"bulk-{i}")
        body = login(client, staff_user).get(reverse("organizations:list")).content.decode()
        assert "Page 1 of 2" in body
        assert "Bulk 24" not in login(client, staff_user).get(reverse("organizations:list"), {"page": 1}).content.decode()


class TestDetail:
    def test_member_can_view_their_organization(self, client, viewer_a, org_a):
        response = login(client, viewer_a).get(reverse("organizations:detail", args=[org_a.pk]))
        assert response.status_code == 200 and b"Org A" in response.content

    def test_other_organization_is_404(self, client, admin_a, org_b):
        assert login(client, admin_a).get(reverse("organizations:detail", args=[org_b.pk])).status_code == 404

    def test_suspended_organization_is_404_for_members(self, client, admin_a, org_a):
        services.suspend_organization(org_a)
        assert login(client, admin_a).get(reverse("organizations:detail", args=[org_a.pk])).status_code == 404

    def test_staff_see_a_suspended_organization_with_a_warning(self, client, staff_user, org_a):
        services.suspend_organization(org_a)
        body = login(client, staff_user).get(reverse("organizations:detail", args=[org_a.pk])).content.decode()
        assert "suspended" in body and "cannot be changed until it is reactivated" in body

    def test_actions_follow_permissions(self, client, viewer_a, admin_a, staff_user, org_a):
        url = reverse("organizations:detail", args=[org_a.pk])
        edit = reverse("organizations:update", args=[org_a.pk])
        status = reverse("organizations:status", args=[org_a.pk])
        viewer = login(client, viewer_a).get(url).content.decode()
        assert edit not in viewer and status not in viewer
        client.logout()
        admin = login(client, admin_a).get(url).content.decode()
        assert edit in admin and status not in admin  # org admins cannot change status
        client.logout()
        staff = login(client, staff_user).get(url).content.decode()
        assert edit in staff and status in staff

    def test_invalid_uuid_in_the_url_is_a_404(self, client, admin_a):
        assert login(client, admin_a).get("/organizations/not-a-uuid/").status_code == 404

    def test_recent_activity_is_not_shown(self, client, staff_user, org_a):
        services.update_organization(org_a, actor=staff_user, phone="1")
        body = login(client, staff_user).get(reverse("organizations:detail", args=[org_a.pk])).content.decode()
        assert "Recent activity" not in body


class TestCreate:
    url = staticmethod(lambda: reverse("organizations:create"))
    data = {"name": "Fresh Org", "code": "", "slug": "", "country": "gb"}

    def test_org_admin_is_forbidden(self, client, admin_a):
        assert login(client, admin_a).get(self.url()).status_code == 403
        assert login(client, admin_a).post(self.url(), self.data).status_code == 403
        assert not Organization.objects.filter(name="Fresh Org").exists()

    def test_anonymous_is_redirected_to_login(self, client):
        assert client.get(self.url()).status_code == 302

    def test_staff_can_create(self, client, staff_user):
        response = login(client, staff_user).post(self.url(), self.data)
        org = Organization.objects.get(name="Fresh Org")
        assert response.status_code == 302 and response["Location"] == org.get_absolute_url()
        assert (org.code, org.country) == ("FRESH_ORG", "GB")
        assert ActivityLog.objects.filter(verb="organization.created", actor=staff_user).exists()

    def test_duplicate_code_shows_a_field_error_and_keeps_input(self, client, staff_user, org_a):
        response = login(client, staff_user).post(self.url(), {**self.data, "code": "org_a", "name": "Keep me"})
        body = response.content.decode()
        assert response.status_code == 200
        assert "already exists" in body and 'value="Keep me"' in body
        assert Organization.objects.filter(name="Keep me").count() == 0

    def test_invalid_timezone_is_reported(self, client, staff_user):
        body = login(client, staff_user).post(self.url(), {**self.data, "timezone": "Nope/Nope"}).content.decode()
        assert "not a recognised IANA time zone" in body


class TestUpdate:
    def test_admin_can_edit_their_organization(self, client, admin_a, org_a):
        url = reverse("organizations:update", args=[org_a.pk])
        response = login(client, admin_a).post(url, {"name": "Org A Renamed", "phone": "555"})
        assert response.status_code == 302
        org_a.refresh_from_db()
        assert (org_a.name, org_a.phone, org_a.code) == ("Org A Renamed", "555", "ORG_A")

    def test_code_cannot_be_changed_by_posting_it(self, client, admin_a, org_a):
        url = reverse("organizations:update", args=[org_a.pk])
        login(client, admin_a).post(url, {"name": "Org A", "code": "HACK"})
        org_a.refresh_from_db()
        assert org_a.code == "ORG_A"

    def test_edit_form_shows_the_code_read_only(self, client, admin_a, org_a):
        body = login(client, admin_a).get(reverse("organizations:update", args=[org_a.pk])).content.decode()
        assert 'name="code"' in body and "disabled" in body.split('name="code"')[1].split(">")[0]

    def test_read_only_member_is_forbidden(self, client, viewer_a, org_a):
        url = reverse("organizations:update", args=[org_a.pk])
        assert login(client, viewer_a).get(url).status_code == 403
        assert client.post(url, {"name": "x"}).status_code == 403

    def test_other_tenants_organization_is_404(self, client, admin_a, org_b):
        url = reverse("organizations:update", args=[org_b.pk])
        assert login(client, admin_a).get(url).status_code == 404
        assert client.post(url, {"name": "pwned"}).status_code == 404
        org_b.refresh_from_db()
        assert org_b.name == "Org B"

    def test_validation_errors_preserve_input(self, client, admin_a, org_a):
        url = reverse("organizations:update", args=[org_a.pk])
        response = login(client, admin_a).post(url, {"name": "Typed name", "currency": "DOLLARS"})
        body = response.content.decode()
        assert response.status_code == 200 and 'value="Typed name"' in body
        org_a.refresh_from_db()
        assert org_a.name == "Org A"

    def test_heading_shows_the_saved_name_not_the_rejected_one(self, client, admin_a, org_a):
        url = reverse("organizations:update", args=[org_a.pk])
        body = login(client, admin_a).post(url, {"name": "Rejected", "currency": "DOLLARS"}).content.decode()
        assert "<title>Edit Org A" in body


class TestStatus:
    def post(self, client, user, org, status):
        return login(client, user).post(reverse("organizations:status", args=[org.pk]), {"status": status})

    def test_staff_can_change_status(self, client, staff_user, org_a):
        assert self.post(client, staff_user, org_a, "inactive").status_code == 302
        org_a.refresh_from_db()
        assert org_a.status == "inactive"
        assert ActivityLog.objects.filter(verb="organization.deactivated", actor=staff_user).exists()

    def test_org_admin_cannot(self, client, admin_a, org_a):
        assert self.post(client, admin_a, org_a, "inactive").status_code == 403
        org_a.refresh_from_db()
        assert org_a.is_active

    def test_unknown_status_is_rejected_with_a_message(self, client, staff_user, org_a):
        response = self.post(client, staff_user, org_a, "gone")
        assert response.status_code == 302
        org_a.refresh_from_db()
        assert org_a.is_active

    def test_get_is_not_allowed(self, client, staff_user, org_a):
        assert login(client, staff_user).get(reverse("organizations:status", args=[org_a.pk])).status_code == 405

    def test_other_tenant_is_404_for_members(self, client, admin_a, org_b):
        assert self.post(client, admin_a, org_b, "inactive").status_code == 404

    def test_csrf_is_enforced(self, admin_a, org_a):
        from django.test import Client

        strict = Client(enforce_csrf_checks=True)
        strict.force_login(admin_a)
        assert strict.post(reverse("organizations:status", args=[org_a.pk]), {"status": "inactive"}).status_code == 403


class TestBrandingPage:
    def test_branding_requires_manage_rights(self, client, viewer_a, org_a):
        assert login(client, viewer_a).get(reverse("organizations:branding", args=[org_a.pk])).status_code == 403

    def test_admin_sees_the_branding_form(self, client, admin_a, org_a):
        response = login(client, admin_a).get(reverse("organizations:branding", args=[org_a.pk]))
        assert response.status_code == 200 and b'enctype="multipart/form-data"' in response.content

    def test_other_tenant_branding_is_404(self, client, admin_a, org_b):
        assert login(client, admin_a).get(reverse("organizations:branding", args=[org_b.pk])).status_code == 404

    def test_brand_colour_is_saved_and_validated(self, client, admin_a, org_a):
        url = reverse("organizations:branding", args=[org_a.pk])
        login(client, admin_a).post(url, {"primary_color": "#4f46e5"})
        org_a.refresh_from_db()
        assert org_a.primary_color == "#4F46E5"
        body = client.post(url, {"primary_color": "red"}).content.decode()
        assert "#RRGGBB" in body
