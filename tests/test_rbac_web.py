"""RBAC at the edges: policies (escalation), the web screens, scoped location access, the API."""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.core.models import ActivityLog
from apps.locations.models import Location
from apps.organizations.models import OrganizationMembership
from apps.permissions import policies, services
from apps.permissions.models import MembershipRole, Role
from apps.permissions.registry import P

SCOPE = MembershipRole.ScopeType


def body(response) -> str:
    return response.content.decode()


@pytest.fixture
def world(org_a, org_b, make_user, assign, make_type, make_location):
    """Org A: a tree; an administrator, a scoped manager, a plain member and a viewer; Org B: an admin."""
    node = make_type(org_a, "Node", "NODE")
    country = make_location(org_a, "Global HQ", type=node)
    r1 = make_location(org_a, "Region 1", parent=country, type=node)
    r2 = make_location(org_a, "Region 2", parent=country, type=node)
    a1 = make_location(org_a, "Area 1", parent=r1, type=node)
    a2 = make_location(org_a, "Area 2", parent=r2, type=node)
    admin = make_user(email="rbac-admin@example.com", organization=org_a, role=None)
    assign(admin, org_a, "administrator")
    scoped = make_user(email="scoped@example.com", organization=org_a, role=None)
    assign(scoped, org_a, "operations-manager", scope=SCOPE.LOCATION_DESCENDANTS, location=r1)
    plain = make_user(email="plain@example.com", organization=org_a, role=None)
    viewer = make_user(email="viewer@example.com", organization=org_a, role=None)
    assign(viewer, org_a, "report-viewer")
    admin_b = make_user(email="rbac-admin-b@example.com", organization=org_b, role=None)
    assign(admin_b, org_b, "administrator")
    return {"node": node, "country": country, "r1": r1, "r2": r2, "a1": a1, "a2": a2,
            "admin": admin, "scoped": scoped, "plain": plain, "viewer": viewer, "admin_b": admin_b}


def member_of(user, org) -> OrganizationMembership:
    return OrganizationMembership.objects.get(user=user, organization=org)


def role(slug: str) -> Role:
    services.seed_system_roles()
    return Role.objects.get(slug=slug, organization=None)


# ---------------------------------------------------------------------------
# Policies: no escalation
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestPolicies:
    def test_cannot_create_a_role_with_permissions_you_do_not_hold(self, world, org_a, assign):
        helper = world["plain"]
        assign(helper, org_a, services.create_role(organization=org_a, name="RoleMaker",
                                                   permissions=[P.ROLE_VIEW, P.ROLE_CREATE, P.LOCATION_VIEW]))
        assert policies.can_create_role(helper, org_a, [P.LOCATION_VIEW])
        assert not policies.can_create_role(helper, org_a, [P.LOCATION_VIEW, P.ROLE_ASSIGN])

    def test_cannot_assign_a_role_more_powerful_than_yourself(self, world, org_a):
        assigner = world["admin"]
        # Make a weaker assigner: role.assign + user.view only.
        weak = world["plain"]
        services.assign_role(member_of(weak, org_a),
                             services.create_role(organization=org_a, name="Weak", permissions=[P.ROLE_ASSIGN, P.USER_VIEW]))
        assert policies.can_assign_role(assigner, member_of(world["viewer"], org_a), role("auditor"))
        assert not policies.can_assign_role(weak, member_of(world["viewer"], org_a), role("administrator"))
        assert not policies.can_assign_role(weak, member_of(world["viewer"], org_a), role("auditor"))  # lacks location.view

    def test_nobody_assigns_themselves_a_role(self, world, org_a):
        assert not policies.can_assign_role(world["admin"], member_of(world["admin"], org_a), role("administrator"))

    def test_only_a_superuser_can_hand_out_super_administrator(self, world, org_a, superuser):
        member = member_of(world["viewer"], org_a)
        assert not policies.can_assign_role(world["admin"], member, role("super-administrator"))

    def test_role_of_another_organization_cannot_be_assigned(self, world, org_a, org_b):
        foreign = services.create_role(organization=org_b, name="Foreign")
        assert not policies.can_assign_role(world["admin"], member_of(world["viewer"], org_a), foreign)

    def test_scoped_assigner_cannot_grant_outside_their_scope(self, world, org_a, assign):
        scoped = world["scoped"]
        assign(scoped, org_a, services.create_role(organization=org_a, name="Delegate",
                                                   permissions=[P.ROLE_ASSIGN, P.LOCATION_VIEW]),
               scope=SCOPE.LOCATION_DESCENDANTS, location=world["r1"])
        target = member_of(world["viewer"], org_a)
        viewer_role = role("auditor")
        assert policies.can_assign_role(scoped, target, viewer_role, scope_type=SCOPE.LOCATION, location=world["a1"])
        assert not policies.can_assign_role(scoped, target, viewer_role, scope_type=SCOPE.LOCATION, location=world["a2"])
        assert not policies.can_assign_role(scoped, target, viewer_role)  # organization-wide needs more

    def test_system_roles_are_not_editable_by_anyone(self, world):
        assert not policies.can_edit_role(world["admin"], role("auditor"))
        assert not policies.can_deactivate_role(world["admin"], role("auditor"))

    def test_other_tenants_roles_are_not_visible(self, world, org_a, org_b):
        foreign = services.create_role(organization=org_b, name="Foreign")
        assert not policies.can_view_role(world["admin"], foreign, org_a)


# ---------------------------------------------------------------------------
# Web: roles
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestRoleScreens:
    def test_anonymous_is_sent_to_sign_in(self, client):
        response = client.get(reverse("access:role_list"))
        assert response.status_code == 302 and "login" in response["Location"]

    def test_list_shows_system_and_custom_roles(self, client, world, org_a):
        services.create_role(organization=org_a, name="Site Lead")
        client.force_login(world["admin"])
        html = body(client.get(reverse("access:role_list")))
        assert "Site Lead" in html and "Administrator" in html and "System" in html and "Custom" in html

    def test_member_without_role_view_gets_the_access_restricted_page(self, client, world):
        client.force_login(world["viewer"])
        response = client.get(reverse("access:role_list"))
        assert response.status_code == 403
        assert "Access restricted" in body(response)

    def test_a_member_with_no_roles_at_all_is_denied(self, client, world):
        client.force_login(world["plain"])
        assert client.get(reverse("access:role_list")).status_code == 403
        assert client.get(reverse("access:member_list")).status_code == 403

    def test_other_tenants_role_is_404(self, client, world, org_b):
        foreign = services.create_role(organization=org_b, name="Foreign")
        client.force_login(world["admin"])
        assert client.get(reverse("access:role_detail", args=[foreign.pk])).status_code == 404

    def test_detail_renders_the_matrix(self, client, world):
        client.force_login(world["admin"])
        html = body(client.get(reverse("access:role_detail", args=[role("auditor").pk])))
        assert 'aria-label="Permission matrix"' in html
        assert "location.view" in html or "View locations" in html
        assert "system role" in html

    def test_create_role_through_the_form(self, client, world, org_a):
        client.force_login(world["admin"])
        response = client.post(reverse("access:role_create"), {
            "name": "Site Lead", "description": "Leads", "permissions": [P.LOCATION_VIEW, P.LOCATION_UPDATE],
        })
        created = Role.objects.get(name="Site Lead")
        assert response.status_code == 302 and created.organization == org_a
        assert set(created.role_permissions.values_list("permission__code", flat=True)) == {P.LOCATION_VIEW, P.LOCATION_UPDATE}

    def test_create_with_unknown_permission_is_invalid(self, client, world):
        client.force_login(world["admin"])
        response = client.post(reverse("access:role_create"), {"name": "Bad", "permissions": ["location.explode"]})
        assert response.status_code == 200 and not Role.objects.filter(name="Bad").exists()

    def test_escalation_through_the_form_is_blocked(self, client, world, org_a, assign):
        helper = world["plain"]
        assign(helper, org_a, services.create_role(organization=org_a, name="RoleMaker",
                                                   permissions=[P.ROLE_VIEW, P.ROLE_CREATE]))
        client.force_login(helper)
        response = client.post(reverse("access:role_create"), {"name": "Sneaky", "permissions": [P.ROLE_ASSIGN]})
        assert response.status_code == 200
        assert not Role.objects.filter(name="Sneaky").exists()

    def test_edit_a_custom_role(self, client, world, org_a):
        custom = services.create_role(organization=org_a, name="Lead", permissions=[P.LOCATION_VIEW])
        client.force_login(world["admin"])
        assert client.get(reverse("access:role_update", args=[custom.pk])).status_code == 200
        client.post(reverse("access:role_update", args=[custom.pk]),
                    {"name": "Lead 2", "description": "", "permissions": [P.LOCATION_UPDATE]})
        custom.refresh_from_db()
        assert custom.name == "Lead 2"
        assert list(custom.role_permissions.values_list("permission__code", flat=True)) == [P.LOCATION_UPDATE]

    def test_system_role_edit_is_forbidden(self, client, world):
        client.force_login(world["admin"])
        target = role("auditor")
        assert client.get(reverse("access:role_update", args=[target.pk])).status_code == 403
        assert client.post(reverse("access:role_update", args=[target.pk]), {"name": "Hacked"}).status_code == 403
        target.refresh_from_db()
        assert target.name == "Auditor"

    def test_deactivate_and_reactivate(self, client, world, org_a):
        custom = services.create_role(organization=org_a, name="Lead")
        client.force_login(world["admin"])
        client.post(reverse("access:role_status", args=[custom.pk]), {"action": "deactivate"})
        custom.refresh_from_db()
        assert not custom.is_active
        client.post(reverse("access:role_status", args=[custom.pk]), {"action": "activate"})
        custom.refresh_from_db()
        assert custom.is_active

    def test_status_is_post_only(self, client, world, org_a):
        custom = services.create_role(organization=org_a, name="Lead")
        client.force_login(world["admin"])
        assert client.get(reverse("access:role_status", args=[custom.pk])).status_code == 405


# ---------------------------------------------------------------------------
# Web: members and assignment
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestMemberScreens:
    def test_member_list_shows_roles_and_status(self, client, world):
        client.force_login(world["admin"])
        html = body(client.get(reverse("access:member_list")))
        assert "viewer@example.com" in html and "Report Viewer" in html and "rbac-admin-b@example.com" not in html

    def test_search_and_status_filter(self, client, world, org_a):
        services.change_membership_status(member_of(world["viewer"], org_a), "suspended")
        client.force_login(world["admin"])
        html = body(client.get(reverse("access:member_list"), {"status": "suspended"}))
        assert "viewer@example.com" in html and "plain@example.com" not in html
        html = body(client.get(reverse("access:member_list"), {"q": "scoped"}))
        assert "scoped@example.com" in html and "plain@example.com" not in html

    def test_access_screen_summary_and_scope_breadcrumb(self, client, world, org_a):
        client.force_login(world["admin"])
        html = body(client.get(reverse("access:member_access", args=[member_of(world["scoped"], org_a).pk])))
        assert "Operations Manager" in html and "Region 1" in html and "Global HQ › Region 1" in html
        assert "Access summary" in html

    def test_other_tenants_membership_is_404(self, client, world, org_b):
        client.force_login(world["admin"])
        foreign = member_of(world["admin_b"], org_b)
        assert client.get(reverse("access:member_access", args=[foreign.pk])).status_code == 404
        assert client.post(reverse("access:member_assign", args=[foreign.pk]), {"role": role("auditor").pk}).status_code == 404
        assert client.post(reverse("access:member_status", args=[foreign.pk]), {"status": "suspended"}).status_code == 404

    def test_assign_a_role_org_wide(self, client, world, org_a):
        client.force_login(world["admin"])
        target = member_of(world["plain"], org_a)
        response = client.post(reverse("access:member_assign", args=[target.pk]),
                               {"role": role("auditor").pk, "scope_type": "organization"})
        assert response.status_code == 302
        assert target.role_assignments.filter(role__slug="auditor", is_active=True).exists()
        assert ActivityLog.objects.filter(verb="membership_role.assigned", organization=org_a).exists()

    def test_assign_a_role_to_a_location_subtree(self, client, world, org_a):
        client.force_login(world["admin"])
        target = member_of(world["plain"], org_a)
        client.post(reverse("access:member_assign", args=[target.pk]), {
            "role": role("auditor").pk, "scope_type": "location_descendants", "location": world["r2"].pk,
        })
        assignment = target.role_assignments.get(role__slug="auditor")
        assert assignment.scope_type == SCOPE.LOCATION_DESCENDANTS and assignment.location == world["r2"]

    def test_location_scope_without_a_location_is_rejected(self, client, world, org_a):
        client.force_login(world["admin"])
        target = member_of(world["plain"], org_a)
        client.post(reverse("access:member_assign", args=[target.pk]),
                    {"role": role("auditor").pk, "scope_type": "location"})
        assert not target.role_assignments.exists()

    def test_forged_foreign_location_is_rejected(self, client, world, org_a, org_b, make_location):
        foreign = make_location(org_b, "Foreign")
        client.force_login(world["admin"])
        target = member_of(world["plain"], org_a)
        client.post(reverse("access:member_assign", args=[target.pk]),
                    {"role": role("auditor").pk, "scope_type": "location", "location": foreign.pk})
        assert not target.role_assignments.exists()

    def test_forged_foreign_role_is_rejected(self, client, world, org_a, org_b):
        foreign = services.create_role(organization=org_b, name="Foreign", permissions=[P.LOCATION_VIEW])
        client.force_login(world["admin"])
        target = member_of(world["plain"], org_a)
        client.post(reverse("access:member_assign", args=[target.pk]), {"role": foreign.pk, "scope_type": "organization"})
        assert not target.role_assignments.exists()

    def test_privilege_escalation_is_forbidden(self, client, world, org_a):
        weak = world["plain"]
        services.assign_role(member_of(weak, org_a),
                             services.create_role(organization=org_a, name="Weak", permissions=[P.ROLE_ASSIGN, P.USER_VIEW]))
        client.force_login(weak)
        target = member_of(world["viewer"], org_a)
        response = client.post(reverse("access:member_assign", args=[target.pk]),
                               {"role": role("administrator").pk, "scope_type": "organization"})
        assert response.status_code == 403
        assert not target.role_assignments.filter(role__slug="administrator").exists()

    def test_self_assignment_is_forbidden(self, client, world, org_a):
        client.force_login(world["admin"])
        me = member_of(world["admin"], org_a)
        response = client.post(reverse("access:member_assign", args=[me.pk]),
                               {"role": role("super-administrator").pk, "scope_type": "organization"})
        assert response.status_code == 403
        html = body(client.get(reverse("access:member_access", args=[me.pk])))
        assert "cannot change your own access" in html

    def test_a_viewer_cannot_assign(self, client, world, org_a):
        client.force_login(world["viewer"])
        response = client.post(reverse("access:member_assign", args=[member_of(world["plain"], org_a).pk]),
                               {"role": role("auditor").pk, "scope_type": "organization"})
        assert response.status_code == 403

    def test_remove_a_role(self, client, world, org_a):
        client.force_login(world["admin"])
        assignment = member_of(world["scoped"], org_a).role_assignments.get()
        response = client.post(reverse("access:assignment_remove", args=[assignment.pk]))
        assert response.status_code == 302
        assignment.refresh_from_db()
        assert not assignment.is_active

    def test_removing_another_tenants_assignment_is_404(self, client, world, org_b):
        client.force_login(world["admin"])
        foreign = member_of(world["admin_b"], org_b).role_assignments.get()
        assert client.post(reverse("access:assignment_remove", args=[foreign.pk])).status_code == 404

    def test_change_membership_status_takes_access_away(self, client, world, org_a):
        target = member_of(world["viewer"], org_a)
        client.force_login(world["admin"])
        client.post(reverse("access:member_status", args=[target.pk]), {"status": "suspended"})
        target.refresh_from_db()
        assert target.status == "suspended"
        client.logout()
        client.force_login(world["viewer"])
        # A suspended member is out of the tenant: nothing in it is reachable.
        assert client.get(reverse("locations:list")).status_code in (302, 403, 404) or "Global HQ" not in body(
            client.get(reverse("locations:list"))
        )

    def test_a_member_cannot_change_their_own_status(self, client, world, org_a):
        client.force_login(world["admin"])
        me = member_of(world["admin"], org_a)
        assert client.post(reverse("access:member_status", args=[me.pk]), {"status": "left"}).status_code == 403

    def test_my_access_page(self, client, world):
        client.force_login(world["viewer"])
        html = body(client.get(reverse("access:my_access")))
        assert "My access" in html and "Access summary" in html


# ---------------------------------------------------------------------------
# Navigation is presentation only
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestNavigation:
    def test_admin_sees_users_and_roles(self, client, world):
        client.force_login(world["admin"])
        html = body(client.get(reverse("core:home")))
        assert reverse("access:role_list") in html and reverse("access:member_list") in html

    def test_viewer_does_not_see_them_and_is_still_forbidden(self, client, world):
        client.force_login(world["viewer"])
        html = body(client.get(reverse("core:home")))
        assert reverse("access:role_list") not in html
        assert client.get(reverse("access:role_list")).status_code == 403


# ---------------------------------------------------------------------------
# Scoped location access (web)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestScopedLocationsWeb:
    def test_tree_shows_only_the_scope_and_hides_ancestor_names(self, client, world):
        client.force_login(world["scoped"])
        html = body(client.get(reverse("locations:tree")))
        assert "Region 1" in html and "Area 1" in html
        assert "Region 2" not in html and "Area 2" not in html and "Global HQ" not in html

    def test_list_is_scoped(self, client, world):
        client.force_login(world["scoped"])
        html = body(client.get(reverse("locations:list")))
        assert "Area 1" in html and "Area 2" not in html

    def test_out_of_scope_detail_is_404_not_403(self, client, world):
        client.force_login(world["scoped"])
        assert client.get(reverse("locations:detail", args=[world["a2"].pk])).status_code == 404
        assert client.get(reverse("locations:detail", args=[world["country"].pk])).status_code == 404
        assert client.get(reverse("locations:detail", args=[world["a1"].pk])).status_code == 200

    def test_detail_hides_the_parent_above_the_scope(self, client, world):
        client.force_login(world["scoped"])
        html = body(client.get(reverse("locations:detail", args=[world["r1"].pk])))
        assert "Global HQ" not in html

    def test_cannot_edit_out_of_scope(self, client, world):
        client.force_login(world["scoped"])
        assert client.get(reverse("locations:update", args=[world["a2"].pk])).status_code == 404
        assert client.post(reverse("locations:update", args=[world["a2"].pk]), {"name": "X"}).status_code == 404

    def test_can_edit_inside_scope(self, client, world):
        client.force_login(world["scoped"])
        assert client.get(reverse("locations:update", args=[world["a1"].pk])).status_code == 200

    def test_view_only_scope_cannot_edit_inside_it(self, client, world, org_a, assign):
        user = world["plain"]
        assign(user, org_a, "auditor", scope=SCOPE.LOCATION_DESCENDANTS, location=world["r1"])
        client.force_login(user)
        assert client.get(reverse("locations:detail", args=[world["a1"].pk])).status_code == 200
        assert client.get(reverse("locations:update", args=[world["a1"].pk])).status_code == 403

    def test_create_under_scope_is_allowed_and_root_is_not(self, client, world, org_a):
        client.force_login(world["scoped"])
        data = {"organization": org_a.pk, "location_type": world["node"].pk, "name": "New Site", "code": "NEW1",
                "parent": world["a1"].pk, "timezone": "UTC", "country": ""}
        client.post(reverse("locations:create"), data)
        assert Location.objects.filter(name="New Site", parent=world["a1"]).exists()
        client.post(reverse("locations:create"), {**data, "name": "Root Site", "code": "ROOT1", "parent": ""})
        assert not Location.objects.filter(name="Root Site").exists()
        client.post(reverse("locations:create"), {**data, "name": "Foreign Site", "code": "OUT1", "parent": world["a2"].pk})
        assert not Location.objects.filter(name="Foreign Site").exists()

    def test_cannot_move_a_location_out_of_scope(self, client, world):
        client.force_login(world["scoped"])
        response = client.post(reverse("locations:update", args=[world["a1"].pk]), {
            "organization": world["a1"].organization_id, "location_type": world["node"].pk, "name": "Area 1",
            "code": world["a1"].code, "parent": world["a2"].pk, "timezone": "UTC",
        })
        world["a1"].refresh_from_db()
        assert response.status_code in (200, 403) and world["a1"].parent_id == world["r1"].pk

    def test_scoped_manager_cannot_deactivate_outside_scope(self, client, world):
        client.force_login(world["scoped"])
        response = client.post(reverse("locations:status", args=[world["a2"].pk]), {"action": "deactivate"})
        assert response.status_code == 404

    def test_org_wide_view_sees_everything(self, client, world):
        client.force_login(world["viewer"])
        html = body(client.get(reverse("locations:list")))
        assert "Area 1" in html and "Area 2" in html

    def test_member_with_no_role_sees_no_locations(self, client, world):
        client.force_login(world["plain"])
        html = body(client.get(reverse("locations:list")))
        assert "Area 1" not in html and "Area 2" not in html


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def api(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.mark.django_db
class TestAccessAPI:
    def test_unauthenticated_is_rejected(self):
        assert APIClient().get("/api/v1/roles/").status_code in (401, 403)

    def test_permission_catalogue(self, world):
        data = api(world["admin"]).get("/api/v1/permissions/").json()
        codes = {row["code"] for row in (data["data"] if isinstance(data, dict) and "data" in data else data)}
        assert P.ROLE_ASSIGN in codes and P.LOCATION_VIEW in codes

    def test_permission_catalogue_requires_permission(self, world):
        assert api(world["viewer"]).get("/api/v1/permissions/").status_code == 403

    def test_roles_list_and_forged_organization(self, world, org_b):
        response = api(world["admin"]).get("/api/v1/roles/")
        assert response.status_code == 200
        assert api(world["admin"]).get("/api/v1/roles/", {"organization": str(org_b.pk)}).status_code == 404

    def test_viewer_cannot_list_roles(self, world):
        assert api(world["viewer"]).get("/api/v1/roles/").status_code == 403
        assert api(world["plain"]).get("/api/v1/roles/").status_code == 403

    def test_create_update_deactivate_role(self, world, org_a):
        client = api(world["admin"])
        created = client.post("/api/v1/roles/", {"name": "Site Lead", "permissions": [P.LOCATION_VIEW]}, format="json")
        assert created.status_code == 201
        role_id = (created.json().get("data") or created.json())["id"]
        updated = client.put(f"/api/v1/roles/{role_id}/update/", {"name": "Site Lead 2", "permissions": [P.LOCATION_UPDATE]}, format="json")
        assert updated.status_code == 200
        assert Role.objects.get(pk=role_id).name == "Site Lead 2"
        assert client.post(f"/api/v1/roles/{role_id}/deactivate/").status_code == 200
        assert not Role.objects.get(pk=role_id).is_active

    def test_unknown_permission_is_a_400(self, world):
        response = api(world["admin"]).post("/api/v1/roles/", {"name": "Bad", "permissions": ["x.y"]}, format="json")
        assert response.status_code == 400

    def test_cannot_edit_a_system_role(self, world):
        target = role("auditor")
        response = api(world["admin"]).put(f"/api/v1/roles/{target.pk}/update/", {"name": "Hacked"}, format="json")
        assert response.status_code == 403

    def test_other_tenants_role_is_404(self, world, org_b):
        foreign = services.create_role(organization=org_b, name="Foreign")
        assert api(world["admin"]).get(f"/api/v1/roles/{foreign.pk}/").status_code == 404

    def test_members_list_assign_and_remove(self, world, org_a):
        client = api(world["admin"])
        listing = client.get("/api/v1/members/")
        assert listing.status_code == 200
        target = member_of(world["plain"], org_a)
        assigned = client.post(f"/api/v1/members/{target.pk}/roles/assign/",
                               {"role": str(role("auditor").pk), "scope_type": "organization"}, format="json")
        assert assigned.status_code == 201
        assignment_id = (assigned.json().get("data") or assigned.json())["id"]
        assert client.get(f"/api/v1/members/{target.pk}/roles/").status_code == 200
        assert client.delete(f"/api/v1/members/{target.pk}/roles/{assignment_id}/").status_code == 204
        assert not target.role_assignments.filter(is_active=True).exists()

    def test_assign_escalation_is_403(self, world, org_a):
        weak = world["plain"]
        services.assign_role(member_of(weak, org_a),
                             services.create_role(organization=org_a, name="Weak", permissions=[P.ROLE_ASSIGN, P.USER_VIEW]))
        target = member_of(world["viewer"], org_a)
        response = api(weak).post(f"/api/v1/members/{target.pk}/roles/assign/",
                                  {"role": str(role("administrator").pk)}, format="json")
        assert response.status_code == 403

    def test_assign_foreign_role_is_404_and_foreign_location_400(self, world, org_a, org_b, make_location):
        client = api(world["admin"])
        target = member_of(world["plain"], org_a)
        foreign_role = services.create_role(organization=org_b, name="Foreign")
        assert client.post(f"/api/v1/members/{target.pk}/roles/assign/", {"role": str(foreign_role.pk)}, format="json").status_code == 404
        foreign_location = make_location(org_b, "Foreign")
        response = client.post(f"/api/v1/members/{target.pk}/roles/assign/", {
            "role": str(role("auditor").pk), "scope_type": "location", "location": str(foreign_location.pk)}, format="json")
        assert response.status_code == 400

    def test_foreign_member_is_404(self, world, org_b):
        foreign = member_of(world["admin_b"], org_b)
        assert api(world["admin"]).get(f"/api/v1/members/{foreign.pk}/").status_code == 404

    def test_status_change(self, world, org_a):
        target = member_of(world["viewer"], org_a)
        response = api(world["admin"]).post(f"/api/v1/members/{target.pk}/status/", {"status": "suspended"}, format="json")
        assert response.status_code == 200
        target.refresh_from_db()
        assert target.status == "suspended"

    def test_my_access_reports_scope(self, world, org_a):
        data = api(world["scoped"]).get("/api/v1/access/me/").json()
        data = data.get("data") or data
        grant = data["permissions"][P.LOCATION_UPDATE]
        assert grant["organization"] is False and grant["locations"]


@pytest.mark.django_db
class TestScopedLocationsAPI:
    def test_list_is_scoped(self, world):
        rows = api(world["scoped"]).get("/api/v1/locations/").json()
        rows = rows.get("data", rows)
        names = {row["name"] for row in rows}
        assert names == {"Region 1", "Area 1"}

    def test_out_of_scope_detail_is_404(self, world):
        client = api(world["scoped"])
        assert client.get(f"/api/v1/locations/{world['a2'].pk}/").status_code == 404
        assert client.get(f"/api/v1/locations/{world['a1'].pk}/").status_code == 200

    def test_parent_above_the_scope_is_masked(self, world):
        data = api(world["scoped"]).get(f"/api/v1/locations/{world['r1'].pk}/").json()
        data = data.get("data", data)
        assert data["parent"] is None and data["parent_name"] is None

    def test_tree_root_is_the_scope_root(self, world):
        tree = api(world["scoped"]).get("/api/v1/locations/tree/").json()
        tree = tree.get("data", tree)
        assert [node["name"] for node in tree] == ["Region 1"] and tree[0]["parent"] is None

    def test_write_outside_scope_is_404_and_view_only_is_403(self, world, org_a, assign):
        client = api(world["scoped"])
        assert client.patch(f"/api/v1/locations/{world['a2'].pk}/", {"name": "X"}, format="json").status_code == 404
        user = world["plain"]
        assign(user, org_a, "auditor", scope=SCOPE.LOCATION_DESCENDANTS, location=world["r1"])
        assert api(user).patch(f"/api/v1/locations/{world['a1'].pk}/", {"name": "X"}, format="json").status_code == 403

    def test_create_root_is_forbidden_under_scope_is_allowed(self, world, org_a):
        client = api(world["scoped"])
        base = {"organization": str(org_a.pk), "location_type": str(world["node"].pk), "name": "N", "code": "N1"}
        assert client.post("/api/v1/locations/", {**base, "parent": str(world["a1"].pk)}, format="json").status_code == 201
        assert client.post("/api/v1/locations/", {**base, "code": "N2"}, format="json").status_code == 403

    def test_move_out_of_scope_is_forbidden(self, world):
        response = api(world["scoped"]).post(f"/api/v1/locations/{world['a1'].pk}/move/",
                                             {"parent": str(world["a2"].pk)}, format="json")
        assert response.status_code in (400, 403, 404)
        world["a1"].refresh_from_db()
        assert world["a1"].parent_id == world["r1"].pk

    def test_users_endpoint_needs_user_view(self, world):
        rows = api(world["viewer"]).get("/api/v1/users/").json()
        rows = rows.get("data", rows)
        assert {row["email"] for row in rows} == {"viewer@example.com"}  # only themselves
        rows = api(world["admin"]).get("/api/v1/users/").json()
        rows = rows.get("data", rows)
        assert "plain@example.com" in {row["email"] for row in rows}
        assert "rbac-admin-b@example.com" not in {row["email"] for row in rows}
