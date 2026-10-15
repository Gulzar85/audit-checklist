"""Cross-tenant isolation, exhaustively.

Organization A's users must never read or change Organization B's data - through
the HTML UI, the REST API, direct object URLs, filters, relations, imports or the
admin - and vice versa.  Hiding things from dropdowns is not enough, so every
object-level URL and action is probed directly.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.locations import services
from apps.locations.models import Location, LocationType
from apps.organizations.models import Organization

pytestmark = pytest.mark.django_db

SENTINEL_A = "ZZ-SENTINEL-ALPHA"
SENTINEL_B = "ZZ-SENTINEL-BRAVO"


@pytest.fixture
def tenants(org_a, org_b, make_type, make_location, admin_a, admin_b):
    """Two fully populated tenants, each with a user who administers only their own."""
    data = {}
    for key, org, user, sentinel in (("a", org_a, admin_a, SENTINEL_A), ("b", org_b, admin_b, SENTINEL_B)):
        location_type = make_type(org, f"{sentinel} Type", f"{key.upper()}TYPE")
        root = make_location(org, f"{sentinel} Root", type=location_type, code=f"{key.upper()}ROOT", city=sentinel)
        child = make_location(org, f"{sentinel} Child", parent=root, type=location_type, code=f"{key.upper()}CHILD")
        data[key] = {"org": org, "user": user, "type": location_type, "root": root, "child": child}
    return data


def web_object_requests(target: dict):
    """(method, url, payload) for every object-level web endpoint of one tenant."""
    org, root, lt = target["org"], target["root"], target["type"]
    return [
        ("get", reverse("organizations:detail", args=[org.pk]), None),
        ("get", reverse("organizations:update", args=[org.pk]), None),
        ("post", reverse("organizations:update", args=[org.pk]), {"name": "pwned"}),
        ("get", reverse("organizations:branding", args=[org.pk]), None),
        ("post", reverse("organizations:branding", args=[org.pk]), {"primary_color": "#000000"}),
        ("post", reverse("organizations:status", args=[org.pk]), {"status": "suspended"}),
        ("get", reverse("locations:detail", args=[root.pk]), None),
        ("get", reverse("locations:update", args=[root.pk]), None),
        ("post", reverse("locations:update", args=[root.pk]), {"name": "pwned"}),
        ("post", reverse("locations:status", args=[root.pk]), {"action": "deactivate", "cascade": "1"}),
        ("get", reverse("locations:type_update", args=[lt.pk]), None),
        ("post", reverse("locations:type_update", args=[lt.pk]), {"name": "pwned"}),
        ("post", reverse("locations:type_status", args=[lt.pk]), {"action": "deactivate"}),
    ]


def api_object_requests(target: dict):
    org, root, lt = target["org"], target["root"], target["type"]
    return [
        ("get", f"/api/v1/organizations/{org.pk}/", None),
        ("patch", f"/api/v1/organizations/{org.pk}/", {"name": "pwned"}),
        ("put", f"/api/v1/organizations/{org.pk}/", {"name": "pwned"}),
        ("post", f"/api/v1/organizations/{org.pk}/status/", {"status": "suspended"}),
        ("get", f"/api/v1/locations/{root.pk}/", None),
        ("patch", f"/api/v1/locations/{root.pk}/", {"name": "pwned"}),
        ("put", f"/api/v1/locations/{root.pk}/", {"name": "pwned"}),
        ("post", f"/api/v1/locations/{root.pk}/move/", {"parent": None}),
        ("post", f"/api/v1/locations/{root.pk}/deactivate/", {"cascade": True}),
        ("post", f"/api/v1/locations/{root.pk}/activate/", {}),
        ("get", f"/api/v1/location-types/{lt.pk}/", None),
        ("patch", f"/api/v1/location-types/{lt.pk}/", {"name": "pwned"}),
        ("post", f"/api/v1/location-types/{lt.pk}/deactivate/", {}),
        ("post", f"/api/v1/location-types/{lt.pk}/activate/", {}),
    ]


def snapshot(tenants):
    return (
        sorted(Organization.objects.values_list("name", "status", "primary_color")),
        sorted(Location.objects.values_list("name", "is_active", "parent_id")),
        sorted(LocationType.objects.values_list("name", "is_active")),
    )


@pytest.mark.parametrize(("attacker", "victim"), [("a", "b"), ("b", "a")])
class TestDirectObjectAccess:
    def test_web_object_endpoints_return_404_and_change_nothing(self, client, tenants, attacker, victim):
        before = snapshot(tenants)
        client.force_login(tenants[attacker]["user"])
        for method, url, payload in web_object_requests(tenants[victim]):
            response = getattr(client, method)(url, payload or {})
            assert response.status_code == 404, f"{method.upper()} {url} -> {response.status_code}"
        assert snapshot(tenants) == before

    def test_api_object_endpoints_return_404_and_change_nothing(self, tenants, attacker, victim):
        before = snapshot(tenants)
        api = APIClient()
        api.force_authenticate(tenants[attacker]["user"])
        for method, url, payload in api_object_requests(tenants[victim]):
            response = getattr(api, method)(url, payload or {}, format="json")
            assert response.status_code == 404, f"{method.upper()} {url} -> {response.status_code}"
            assert response.json()["error"]["code"] == "NOT_FOUND"
        assert snapshot(tenants) == before

    def test_the_attacker_can_still_use_their_own_objects(self, client, tenants, attacker, victim):
        """Control: the same endpoints work (not 404) against the user's own tenant."""
        client.force_login(tenants[attacker]["user"])
        own = tenants[attacker]
        assert client.get(reverse("locations:detail", args=[own["root"].pk])).status_code == 200
        api = APIClient()
        api.force_authenticate(own["user"])
        assert api.get(f"/api/v1/locations/{own['root'].pk}/").status_code == 200


@pytest.mark.parametrize(("attacker", "victim"), [("a", "b"), ("b", "a")])
class TestNoDataLeaks:
    SENTINELS = {"a": SENTINEL_A, "b": SENTINEL_B}

    def test_no_page_or_fragment_mentions_the_other_tenant(self, client, tenants, attacker, victim):
        client.force_login(tenants[attacker]["user"])
        secret = self.SENTINELS[victim]
        own = tenants[attacker]
        urls = [
            reverse("organizations:list"),
            reverse("locations:list"),
            reverse("locations:tree"),
            reverse("locations:type_list"),
            reverse("locations:create"),
            reverse("locations:type_create"),
            reverse("organizations:detail", args=[own["org"].pk]),
            reverse("locations:detail", args=[own["root"].pk]),
            reverse("locations:update", args=[own["root"].pk]),
            reverse("locations:picker"),
            reverse("locations:picker") + "?q=sentinel",
            reverse("locations:picker") + "?q=" + secret,
            reverse("locations:form_relations") + f"?organization={tenants[victim]['org'].pk}",
            reverse("locations:list") + f"?q={secret}",
            reverse("locations:tree") + f"?q={secret}",
            reverse("locations:list") + f"?organization={tenants[victim]['org'].pk}",
            reverse("locations:list") + f"?parent={tenants[victim]['root'].pk}",
            reverse("locations:list") + f"?location_type={tenants[victim]['type'].pk}",
            reverse("locations:create") + f"?parent={tenants[victim]['root'].pk}",
            reverse("locations:create") + f"?organization={tenants[victim]['org'].pk}",
        ]
        for url in urls:
            content = client.get(url).content.decode()
            # Victim data always reads "<sentinel> Root/Child/Type"; a search box merely
            # echoing the user's own query reads "<sentinel>" followed by a quote.
            assert f"{secret} " not in content, url
            assert tenants[victim]["root"].code not in content, url
            # An id the user themself put in the URL may be echoed back in a form field;
            # an id they did *not* supply must never appear.
            for ident in (tenants[victim]["root"].pk, tenants[victim]["org"].pk, tenants[victim]["type"].pk):
                assert str(ident) not in content or str(ident) in url, (url, ident)

    def test_no_api_response_mentions_the_other_tenant(self, tenants, attacker, victim):
        api = APIClient()
        api.force_authenticate(tenants[attacker]["user"])
        secret = self.SENTINELS[victim]
        for url in (
            "/api/v1/organizations/", "/api/v1/locations/", "/api/v1/location-types/", "/api/v1/locations/tree/",
            f"/api/v1/locations/?search={secret}", f"/api/v1/locations/tree/?search={secret}",
            f"/api/v1/locations/?organization={tenants[victim]['org'].pk}",
            f"/api/v1/locations/?parent={tenants[victim]['root'].pk}",
            f"/api/v1/locations/?location_type={tenants[victim]['type'].pk}",
            f"/api/v1/location-types/?organization={tenants[victim]['org'].pk}",
        ):
            content = api.get(url).content.decode()
            assert f"{secret} " not in content, url
            assert tenants[victim]["root"].code not in content, url
            assert str(tenants[victim]["root"].pk) not in content, url

    def test_relations_to_the_other_tenant_cannot_be_formed_through_the_api(self, tenants, attacker, victim):
        api = APIClient()
        api.force_authenticate(tenants[attacker]["user"])
        own, other = tenants[attacker], tenants[victim]
        base = {"organization": str(own["org"].pk), "location_type": str(own["type"].pk), "name": "N", "code": "NEW"}
        for field, value in (
            ("parent", other["root"].pk), ("location_type", other["type"].pk), ("organization", other["org"].pk),
        ):
            response = api.post("/api/v1/locations/", {**base, field: str(value)}, format="json")
            assert response.status_code == 400, field
        assert not Location.objects.filter(code="NEW").exists()

    def test_relations_to_the_other_tenant_cannot_be_formed_through_the_ui(self, client, tenants, attacker, victim):
        client.force_login(tenants[attacker]["user"])
        own, other = tenants[attacker], tenants[victim]
        base = {"organization": str(own["org"].pk), "location_type": str(own["type"].pk), "name": "N", "code": "NEW"}
        for field, value in (
            ("parent", other["root"].pk), ("location_type", other["type"].pk), ("organization", other["org"].pk),
        ):
            response = client.post(reverse("locations:create"), {**base, field: str(value)})
            assert response.status_code == 200, field  # re-rendered with errors, never created
        assert not Location.objects.filter(code="NEW").exists()

    def test_existing_locations_cannot_be_re_parented_into_the_other_tenant(self, client, tenants, attacker, victim):
        client.force_login(tenants[attacker]["user"])
        own, other = tenants[attacker], tenants[victim]
        before = own["child"].parent_id
        data = {"organization": str(own["org"].pk), "location_type": str(own["type"].pk),
                "name": own["child"].name, "code": own["child"].code, "parent": str(other["root"].pk)}
        client.post(reverse("locations:update", args=[own["child"].pk]), data)
        own["child"].refresh_from_db()
        assert own["child"].parent_id == before
        api = APIClient()
        api.force_authenticate(own["user"])
        assert api.post(f"/api/v1/locations/{own['child'].pk}/move/", {"parent": str(other["root"].pk)}, format="json").status_code == 400


class TestSuspendedOrganizationIsInert:
    """'Inactive/suspended organizations must not perform normal operations' - on every channel."""

    @pytest.fixture
    def suspended(self, tenants):
        from apps.organizations import services as org_services

        org_services.suspend_organization(tenants["a"]["org"])
        return tenants

    def test_members_lose_ui_access(self, client, suspended):
        client.force_login(suspended["a"]["user"])
        for method, url, payload in web_object_requests(suspended["a"]):
            assert getattr(client, method)(url, payload or {}).status_code == 404, url
        assert SENTINEL_A not in client.get(reverse("locations:tree")).content.decode()

    def test_members_lose_api_access(self, suspended):
        api = APIClient()
        api.force_authenticate(suspended["a"]["user"])
        for method, url, payload in api_object_requests(suspended["a"]):
            assert getattr(api, method)(url, payload or {}, format="json").status_code == 404, url
        assert api.get("/api/v1/locations/").json()["data"] == []

    def test_even_platform_staff_cannot_change_its_hierarchy_until_it_is_reactivated(self, suspended):
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            services.create_location(
                organization=suspended["a"]["org"], location_type=suspended["a"]["type"], name="X", code="X"
            )
        with pytest.raises(ValidationError):
            services.move_location(suspended["a"]["child"], None)
        with pytest.raises(ValidationError):
            services.deactivate_location(suspended["a"]["root"], cascade=True)

    def test_no_data_is_deleted_by_suspension(self, suspended):
        assert Location.objects.filter(organization=suspended["a"]["org"]).count() == 2
        assert LocationType.objects.filter(organization=suspended["a"]["org"]).exists()

    def test_reactivation_restores_access(self, client, suspended):
        from apps.organizations import services as org_services

        org_services.activate_organization(suspended["a"]["org"])
        client.force_login(suspended["a"]["user"])
        assert SENTINEL_A in client.get(reverse("locations:tree")).content.decode()


class TestPlatformStaffAreTheOnlyCrossTenantActors:
    def test_staff_see_both_tenants(self, client, staff_user, tenants):
        client.force_login(staff_user)
        content = client.get(reverse("locations:tree")).content.decode()
        assert SENTINEL_A in content and SENTINEL_B in content

    def test_a_user_in_two_organizations_sees_exactly_those_two(self, client, make_user, tenants, org_a, org_b):
        from apps.organizations.models import OrganizationMembership

        third = Organization.objects.create(name="Third", slug="third")
        user = make_user(organization=org_a)
        OrganizationMembership.objects.create(user=user, organization=org_b)
        client.force_login(user)
        organizations = client.get(reverse("organizations:list")).content.decode()
        assert "Org A" in organizations and "Org B" in organizations and str(third.pk) not in organizations

    def test_deactivated_membership_removes_access(self, client, tenants):
        from apps.organizations.models import OrganizationMembership

        OrganizationMembership.objects.filter(user=tenants["a"]["user"]).update(is_active=False)
        client.force_login(tenants["a"]["user"])
        assert client.get(reverse("locations:detail", args=[tenants["a"]["root"].pk])).status_code == 404
        assert SENTINEL_A not in client.get(reverse("locations:tree")).content.decode()

    def test_admin_is_closed_to_tenant_users(self, client, tenants):
        client.force_login(tenants["a"]["user"])
        for path in ("/admin/", "/admin/locations/location/", "/admin/organizations/organization/"):
            assert client.get(path).status_code == 302
        assert client.get("/admin/locations/location/").url.startswith("/admin/login/")


class TestReviewRegressions:
    def test_type_edit_form_never_lists_other_organizations(self, client, tenants):
        client.force_login(tenants["a"]["user"])
        html = client.get(reverse("locations:type_update", args=[tenants["a"]["type"].pk])).content.decode()
        assert "Org B" not in html and str(tenants["b"]["org"].pk) not in html

    def test_exports_neutralise_spreadsheet_formulas(self, tenants):
        from apps.locations.resources import LocationResource

        Location.objects.filter(pk=tenants["a"]["root"].pk).update(name="=1+1")
        exported = LocationResource().export().csv
        assert "'=1+1" in exported and ",=1+1," not in exported

    def test_import_into_a_suspended_organization_is_refused(self, tenants, staff_user):
        import tablib

        from apps.locations.resources import LocationResource
        from apps.organizations import services as org_services

        org_services.suspend_organization(tenants["a"]["org"])
        data = tablib.Dataset(headers=["organization", "code", "name", "location_type", "parent"])
        data.append([tenants["a"]["org"].code, "late", "Late", tenants["a"]["type"].code, ""])
        result = LocationResource().import_data(
            data, user=staff_user, use_transactions=True, rollback_on_validation_errors=True
        )
        assert result.has_errors() or result.has_validation_errors()
        assert not Location.objects.filter(code="LATE").exists()
