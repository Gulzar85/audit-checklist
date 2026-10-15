"""/api/v1/organizations/ - authentication, permissions, isolation, validation."""

from __future__ import annotations

import uuid

import pytest
from rest_framework.test import APIClient

from apps.core.models import ActivityLog
from apps.organizations import services
from apps.organizations.models import Organization

pytestmark = pytest.mark.django_db


@pytest.fixture
def api():
    def _client(user=None) -> APIClient:
        client = APIClient()
        if user is not None:
            client.force_authenticate(user)
        return client

    return _client


LIST = "/api/v1/organizations/"


def detail(org) -> str:
    return f"{LIST}{org.pk}/"


class TestAuthentication:
    def test_anonymous_requests_are_rejected(self, api, org_a):
        for method, url in (("get", LIST), ("get", detail(org_a)), ("post", LIST)):
            response = getattr(api(), method)(url)
            assert response.status_code in (401, 403)
            assert response.json()["error"]["code"] in ("NOT_AUTHENTICATED", "PERMISSION_DENIED")

    def test_inactive_users_are_rejected(self, api, make_user, org_a):
        user = make_user(organization=org_a, active=False)
        assert api(user).get(LIST).status_code in (401, 403)

    def test_success_uses_the_envelope(self, api, admin_a):
        payload = api(admin_a).get(LIST).json()
        assert set(payload) == {"data", "meta"}
        assert payload["meta"]["count"] == 1


class TestListAndRead:
    def test_member_sees_only_their_organization(self, api, admin_a, org_a, org_b):
        data = api(admin_a).get(LIST).json()["data"]
        assert [o["id"] for o in data] == [str(org_a.pk)]

    def test_staff_sees_every_organization(self, api, staff_user, org_a, org_b):
        data = api(staff_user).get(LIST).json()["data"]
        assert {o["id"] for o in data} == {str(org_a.pk), str(org_b.pk)}

    def test_user_with_no_membership_sees_nothing(self, api, make_user, org_a):
        assert api(make_user()).get(LIST).json()["data"] == []

    def test_retrieve_own(self, api, admin_a, org_a):
        data = api(admin_a).get(detail(org_a)).json()["data"]
        assert data["code"] == org_a.code and data["is_active"] is True

    def test_other_organization_is_404_not_403(self, api, admin_a, org_b):
        response = api(admin_a).get(detail(org_b))
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    def test_invalid_uuid_is_404(self, api, admin_a):
        response = api(admin_a).get(f"{LIST}not-a-uuid/")
        assert response.status_code == 404
        assert "error" in response.json()

    def test_unknown_uuid_is_404(self, api, admin_a):
        assert api(admin_a).get(f"{LIST}{uuid.uuid4()}/").status_code == 404

    def test_suspended_organization_disappears_for_members(self, api, admin_a, org_a):
        services.suspend_organization(org_a)
        assert api(admin_a).get(detail(org_a)).status_code == 404
        assert api(admin_a).get(LIST).json()["data"] == []

    def test_staff_can_still_read_a_suspended_organization(self, api, staff_user, org_a):
        services.suspend_organization(org_a)
        assert api(staff_user).get(detail(org_a)).json()["data"]["status"] == "suspended"


class TestFilteringPaginationOrdering:
    @pytest.fixture
    def many(self):
        for i in range(5):
            Organization.objects.create(name=f"Org {i}", slug=f"org-{i}", country="GB" if i % 2 else "US")
        services.deactivate_organization(Organization.objects.get(slug="org-0"))

    def test_search(self, api, staff_user, many):
        data = api(staff_user).get(LIST, {"search": "org 3"}).json()["data"]
        assert [o["name"] for o in data] == ["Org 3"]

    def test_filter_by_status(self, api, staff_user, many):
        data = api(staff_user).get(LIST, {"status": "inactive"}).json()["data"]
        assert [o["name"] for o in data] == ["Org 0"]

    def test_filter_is_active(self, api, staff_user, many):
        assert len(api(staff_user).get(LIST, {"is_active": "false"}).json()["data"]) == 1
        assert len(api(staff_user).get(LIST, {"is_active": "true"}).json()["data"]) == 4

    def test_filter_by_country(self, api, staff_user, many):
        data = api(staff_user).get(LIST, {"country": "gb"}).json()["data"]
        assert {o["name"] for o in data} == {"Org 1", "Org 3"}

    def test_ordering(self, api, staff_user, many):
        data = api(staff_user).get(LIST, {"ordering": "-name"}).json()["data"]
        assert [o["name"] for o in data][0] == "Org 4"

    def test_pagination(self, api, staff_user, many):
        payload = api(staff_user).get(LIST, {"page_size": 2}).json()
        assert len(payload["data"]) == 2
        assert payload["meta"]["count"] == 5 and payload["meta"]["next"]

    def test_unknown_filter_values_are_rejected_cleanly(self, api, staff_user, many):
        assert api(staff_user).get(LIST, {"status": "nonsense"}).status_code == 400


class TestCreate:
    payload = {"name": "New Org", "country": "gb", "timezone": "Europe/London"}

    def test_staff_can_create(self, api, staff_user):
        response = api(staff_user).post(LIST, self.payload)
        assert response.status_code == 201
        data = response.json()["data"]
        assert (data["code"], data["country"], data["status"]) == ("NEW_ORG", "GB", "active")
        assert ActivityLog.objects.filter(verb="organization.created", actor=staff_user).exists()

    def test_org_admins_cannot_create_organizations(self, api, admin_a):
        assert api(admin_a).post(LIST, self.payload).status_code == 403

    def test_status_in_payload_is_ignored(self, api, staff_user):
        data = api(staff_user).post(LIST, {**self.payload, "status": "suspended"}).json()["data"]
        assert data["status"] == "active"

    def test_duplicate_code_is_a_validation_error(self, api, staff_user, org_a):
        response = api(staff_user).post(LIST, {"name": "Other", "code": org_a.code.lower()})
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "VALIDATION_ERROR" and "code" in error["details"]

    def test_duplicate_slug_is_a_validation_error(self, api, staff_user, org_a):
        response = api(staff_user).post(LIST, {"name": "Other", "slug": org_a.slug})
        assert response.status_code == 400 and "slug" in response.json()["error"]["details"]

    @pytest.mark.parametrize("field, value", [("timezone", "Nowhere/Land"), ("currency", "DOLLARS"), ("primary_color", "blue")])
    def test_invalid_values(self, api, staff_user, field, value):
        response = api(staff_user).post(LIST, {"name": "X", field: value})
        assert response.status_code == 400 and field in response.json()["error"]["details"]

    def test_name_is_required(self, api, staff_user):
        assert "name" in api(staff_user).post(LIST, {}).json()["error"]["details"]


class TestUpdate:
    def test_org_admin_can_patch_their_organization(self, api, admin_a, org_a):
        response = api(admin_a).patch(detail(org_a), {"phone": "123", "city": "Leeds"})
        assert response.status_code == 200
        org_a.refresh_from_db()
        assert (org_a.phone, org_a.city) == ("123", "Leeds")
        assert ActivityLog.objects.filter(verb="organization.updated", actor=admin_a).exists()

    def test_put_requires_the_full_representation(self, api, admin_a, org_a):
        assert api(admin_a).put(detail(org_a), {"phone": "1"}).status_code == 400

    def test_read_only_member_cannot_update(self, api, viewer_a, org_a):
        assert api(viewer_a).patch(detail(org_a), {"phone": "1"}).status_code == 403

    def test_cannot_update_another_organization(self, api, admin_a, org_b):
        assert api(admin_a).patch(detail(org_b), {"phone": "1"}).status_code == 404
        org_b.refresh_from_db()
        assert org_b.phone == ""

    def test_code_is_immutable(self, api, admin_a, org_a):
        response = api(admin_a).patch(detail(org_a), {"code": "CHANGED"})
        assert response.status_code == 400 and "code" in response.json()["error"]["details"]

    def test_status_cannot_be_patched(self, api, admin_a, org_a):
        api(admin_a).patch(detail(org_a), {"status": "suspended"})
        org_a.refresh_from_db()
        assert org_a.is_active

    def test_delete_is_not_offered(self, api, staff_user, org_a):
        assert api(staff_user).delete(detail(org_a)).status_code == 405
        assert Organization.objects.filter(pk=org_a.pk).exists()


class TestStatusAction:
    url = staticmethod(lambda org: f"{detail(org)}status/")

    def test_staff_can_change_status(self, api, staff_user, org_a):
        response = api(staff_user).post(self.url(org_a), {"status": "suspended"})
        assert response.status_code == 200 and response.json()["data"]["status"] == "suspended"
        assert ActivityLog.objects.filter(verb="organization.suspended", actor=staff_user).exists()

    def test_org_admin_cannot_change_status(self, api, admin_a, org_a):
        assert api(admin_a).post(self.url(org_a), {"status": "inactive"}).status_code == 403
        org_a.refresh_from_db()
        assert org_a.is_active

    def test_invalid_status(self, api, staff_user, org_a):
        assert api(staff_user).post(self.url(org_a), {"status": "gone"}).status_code == 400

    def test_other_tenants_status_is_404_for_members(self, api, admin_a, org_b):
        assert api(admin_a).post(self.url(org_b), {"status": "inactive"}).status_code == 404
