"""The policy layer: one rule set shared by the web UI, the API and imports."""

from __future__ import annotations

import pytest
from django.contrib.auth.models import AnonymousUser

from apps.locations import policies as loc
from apps.organizations import policies as org
from apps.organizations import services as org_services
from apps.organizations.selectors import organizations_visible_to

pytestmark = pytest.mark.django_db

ALL_PERMS = (
    "organizations.view_organization", "organizations.change_organization", "organizations.add_organization",
    "locations.view_location", "locations.add_location", "locations.change_location",
    "locations.view_locationtype", "locations.add_locationtype", "locations.change_locationtype",
)


@pytest.fixture
def actors(make_user, org_a, org_b):
    """Every kind of user the policies must tell apart."""
    return {
        "anonymous": AnonymousUser(),
        "inactive_admin": make_user(organization=org_a, perms=ALL_PERMS, active=False),
        "member_no_perms": make_user(organization=org_a),
        "member_with_perms": make_user(organization=org_a, perms=ALL_PERMS),
        "inactive_membership": make_user(organization=org_a, perms=ALL_PERMS, member_active=False),
        "other_tenant_admin": make_user(organization=org_b, perms=ALL_PERMS),
        "no_membership": make_user(perms=ALL_PERMS),
        "staff_no_perms": make_user(staff=True),
        "staff_with_perms": make_user(staff=True, perms=ALL_PERMS),
    }


def allowed(check, actors, *args, **kwargs) -> set[str]:
    return {name for name, user in actors.items() if check(user, *args, **kwargs)}


class TestOrganizationPolicies:
    def test_view(self, actors, org_a):
        assert allowed(org.can_view_organization, actors, org_a) == {
            "member_no_perms", "member_with_perms", "staff_no_perms", "staff_with_perms",
        } | set()

    def test_view_matches_the_visibility_selector(self, actors, org_a):
        """One definition of 'who can see a tenant': the policy and selector agree."""
        for name, user in actors.items():
            assert org.can_view_organization(user, org_a) == organizations_visible_to(user).filter(pk=org_a.pk).exists(), name

    def test_manage_needs_visibility_and_the_model_permission(self, actors, org_a):
        assert allowed(org.can_manage_organization, actors, org_a) == {"member_with_perms", "staff_with_perms"}

    def test_creating_organizations_is_a_platform_operation(self, actors):
        assert allowed(org.can_create_organization, actors) == {"staff_with_perms"}

    def test_status_changes_are_platform_staff_only(self, actors, org_a):
        assert allowed(org.can_change_organization_status, actors, org_a) == {"staff_with_perms"}

    def test_suspended_organizations_are_invisible_to_members_but_not_staff(self, actors, org_a):
        org_services.suspend_organization(org_a)
        assert allowed(org.can_view_organization, actors, org_a) == {"staff_no_perms", "staff_with_perms"}
        assert allowed(org.can_manage_organization, actors, org_a) == {"staff_with_perms"}

    def test_policies_accept_an_id_as_well_as_an_instance(self, actors, org_a):
        assert org.can_view_organization(actors["member_no_perms"], org_a.pk)

    def test_membership_helper(self, actors, org_a, org_b):
        assert org.is_organization_member(actors["member_no_perms"], org_a)
        assert not org.is_organization_member(actors["member_no_perms"], org_b)
        assert not org.is_organization_member(actors["inactive_membership"], org_a)
        assert not org.is_organization_member(actors["anonymous"], org_a)


class TestLocationPolicies:
    def test_view_follows_the_organization(self, actors, org_a, make_location):
        location = make_location(org_a, "L")
        assert allowed(loc.can_view_location, actors, location) == {
            "member_no_perms", "member_with_perms", "staff_no_perms", "staff_with_perms",
        }

    def test_create_in_an_organization(self, actors, org_a):
        assert allowed(loc.can_create_location, actors, org_a) == {"member_with_perms", "staff_with_perms"}

    def test_create_anywhere(self, actors):
        """"Somewhere": anyone with the permission who can reach at least one organization."""
        assert allowed(loc.can_create_location, actors) == {
            "member_with_perms", "other_tenant_admin", "staff_with_perms",
        }

    @pytest.mark.parametrize("check", [loc.can_edit_location, loc.can_deactivate_location, loc.can_activate_location])
    def test_edit_family(self, check, actors, org_a, make_location):
        assert allowed(check, actors, make_location(org_a, "L")) == {"member_with_perms", "staff_with_perms"}

    def test_move_needs_rights_over_the_destination_too(self, actors, org_a, org_b, make_location):
        source = make_location(org_a, "S")
        own_target = make_location(org_a, "T")
        foreign_target = make_location(org_b, "F")
        assert allowed(loc.can_move_location, actors, source, own_target) == {"member_with_perms", "staff_with_perms"}
        assert allowed(loc.can_move_location, actors, source, None) == {"member_with_perms", "staff_with_perms"}
        assert allowed(loc.can_move_location, actors, source, foreign_target) == {"staff_with_perms"}

    def test_other_tenants_admin_has_no_rights_over_a_location(self, actors, org_b, make_location):
        assert "member_with_perms" not in allowed(loc.can_edit_location, actors, make_location(org_b, "B"))

    def test_suspended_organization_removes_member_rights(self, actors, org_a, make_location):
        location = make_location(org_a, "L")
        org_services.suspend_organization(org_a)
        assert allowed(loc.can_edit_location, actors, location) == {"staff_with_perms"}


class TestLocationTypePolicies:
    def test_org_specific_types(self, actors, org_a, make_type):
        lt = make_type(org_a, "T", "T")
        assert allowed(loc.can_create_location_type, actors, org_a) == {"member_with_perms", "staff_with_perms"}
        assert allowed(loc.can_edit_location_type, actors, lt) == {"member_with_perms", "staff_with_perms"}
        assert allowed(loc.can_toggle_location_type, actors, lt) == {"member_with_perms", "staff_with_perms"}

    def test_platform_types_are_platform_staff_only(self, actors, make_type):
        lt = make_type(None, "Platform", "PLATFORM")
        assert allowed(loc.can_create_location_type, actors, None) == {"staff_with_perms"}
        assert allowed(loc.can_edit_location_type, actors, lt) == {"staff_with_perms"}

    def test_create_any(self, actors):
        assert allowed(loc.can_create_any_location_type, actors) == {
            "member_with_perms", "other_tenant_admin", "staff_with_perms",
        }

    def test_a_member_cannot_manage_types_of_another_tenant(self, actors, org_b, make_type):
        lt = make_type(org_b, "T", "T")
        assert "member_with_perms" not in allowed(loc.can_edit_location_type, actors, lt)


class TestPermissionsAreDataNotRoleNames:
    def test_no_policy_inspects_role_names(self):
        """Authorization is permissions + membership + scope, never ``role == 'Administrator'``."""
        import inspect

        for module in (org, loc):
            source = inspect.getsource(module)
            for forbidden in ('"Administrator"', "'Administrator'", ".role ==", "has_role("):
                assert forbidden not in source, (module.__name__, forbidden)
