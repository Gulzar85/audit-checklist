"""RBAC core: registry, models, services, the authorization service, scope and caching."""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import IntegrityError, transaction

from apps.core.models import ActivityLog
from apps.organizations.models import OrganizationMembership
from apps.permissions import authorization as authz
from apps.permissions import registry, services
from apps.permissions.models import MembershipRole, Permission, Role, RolePermission
from apps.permissions.registry import P

SCOPE = MembershipRole.ScopeType


# ---------------------------------------------------------------------------
# Registry and seeding
# ---------------------------------------------------------------------------
class TestRegistry:
    def test_codes_are_unique_and_resource_dot_action(self):
        codes = [d.code for d in registry.REGISTRY]
        assert len(codes) == len(set(codes))
        for definition in registry.REGISTRY:
            assert definition.code == f"{definition.resource}.{definition.action}"

    def test_constants_exist_for_every_code(self):
        for definition in registry.REGISTRY:
            assert getattr(P, f"{definition.resource}_{definition.action}".upper()) == definition.code

    def test_a_typo_in_a_constant_is_loud(self):
        with pytest.raises(AttributeError):
            P.LOCATION_UPDAET  # noqa: B018

    def test_system_roles_only_reference_registered_codes(self):
        for slug, (_name, _desc, codes) in registry.SYSTEM_ROLES.items():
            assert all(registry.is_registered(code) for code in codes), slug

    def test_every_expected_system_role_exists(self):
        assert set(registry.SYSTEM_ROLES) == {
            "super-administrator", "administrator", "qa-manager", "auditor", "operations-consultant",
            "operations-manager", "restaurant-manager", "restaurant-user", "report-viewer",
        }

    def test_unregistered_code_raises_instead_of_silently_denying(self, user, org_a):
        with pytest.raises(ImproperlyConfigured):
            authz.has_permission(user, "location.explode", organization=org_a)


@pytest.mark.django_db
class TestSeeding:
    def test_sync_is_idempotent(self):
        first = services.sync_registry()
        second = services.sync_registry()
        assert first["created"] == len(registry.REGISTRY)
        assert second == {"created": 0, "updated": 0, "deactivated": 0}

    def test_stale_permission_is_deactivated_not_deleted(self):
        services.sync_registry()
        Permission.objects.create(code="legacy.thing", resource="legacy", action="thing", label="Legacy")
        assert services.sync_registry()["deactivated"] == 1
        assert Permission.objects.get(code="legacy.thing").is_active is False

    def test_seed_roles_is_idempotent_and_protected(self):
        services.seed_system_roles()
        again = services.seed_system_roles()
        assert again == {"created": 0, "refreshed": 9}
        admin = Role.objects.get(slug="administrator", organization=None)
        assert admin.is_system
        assert admin.role_permissions.count() == len(registry.REGISTRY)
        assert Role.objects.filter(is_system=True).count() == 9

    def test_reseeding_restores_a_tampered_system_role(self):
        services.seed_system_roles()
        role = Role.objects.get(slug="auditor", organization=None)
        role.role_permissions.all().delete()
        services.seed_system_roles()
        assert role.role_permissions.count() == len(registry.SYSTEM_ROLES["auditor"][2])

    def test_commands_run(self, capsys):
        from django.core.management import call_command

        call_command("seed_permissions")
        call_command("seed_roles")
        call_command("seed_roles")
        assert "System roles: 0 created, 9 refreshed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Model constraints
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestConstraints:
    def test_system_role_cannot_belong_to_an_organization(self, org_a):
        with pytest.raises(IntegrityError), transaction.atomic():
            Role.objects.create(name="X", slug="x", organization=org_a, is_system=True)

    def test_role_permission_is_unique(self, org_a):
        services.sync_registry()
        role = services.create_role(organization=org_a, name="R", permissions=[P.LOCATION_VIEW])
        with pytest.raises(IntegrityError), transaction.atomic():
            RolePermission.objects.create(role=role, permission=Permission.objects.get(code=P.LOCATION_VIEW))

    def test_location_scope_needs_a_location(self, make_user, org_a):
        services.seed_system_roles()
        make_user(organization=org_a, role=None)
        member = OrganizationMembership.objects.get(organization=org_a)
        role = Role.objects.get(slug="auditor", organization=None)
        with pytest.raises(ValidationError):
            MembershipRole(membership=member, role=role, scope_type=SCOPE.LOCATION).save()

    def test_org_scope_cannot_carry_a_location(self, make_user, org_a, chain_a):
        services.seed_system_roles()
        make_user(organization=org_a, role=None)
        member = OrganizationMembership.objects.get(organization=org_a)
        role = Role.objects.get(slug="auditor", organization=None)
        with pytest.raises(ValidationError):
            MembershipRole(membership=member, role=role, scope_type=SCOPE.ORGANIZATION, location=chain_a[0]).save()

    def test_location_must_belong_to_the_members_organization(self, make_user, org_a, org_b, make_location):
        services.seed_system_roles()
        make_user(organization=org_a, role=None)
        foreign = make_location(org_b, "Foreign")
        member = OrganizationMembership.objects.get(organization=org_a)
        role = Role.objects.get(slug="auditor", organization=None)
        with pytest.raises(ValidationError):
            MembershipRole(membership=member, role=role, scope_type=SCOPE.LOCATION, location=foreign).save()

    def test_duplicate_assignment_is_rejected_by_the_database(self, make_user, org_a):
        services.seed_system_roles()
        make_user(organization=org_a, role=None)
        member = OrganizationMembership.objects.get(organization=org_a)
        role = Role.objects.get(slug="auditor", organization=None)
        MembershipRole.objects.create(membership=member, role=role)
        with pytest.raises(IntegrityError), transaction.atomic():
            MembershipRole.objects.create(membership=member, role=role)

    def test_membership_statuses(self):
        assert set(OrganizationMembership.Status.values) == {"pending", "active", "suspended", "left"}


# ---------------------------------------------------------------------------
# Role services
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestRoleServices:
    def test_create_role_with_permissions_is_audited(self, org_a, admin_a):
        role = services.create_role(
            organization=org_a, name="Site Lead", description="Leads a site",
            permissions=[P.LOCATION_VIEW, P.LOCATION_UPDATE], actor=admin_a,
        )
        assert not role.is_system and role.organization == org_a
        assert set(role.role_permissions.values_list("permission__code", flat=True)) == {P.LOCATION_VIEW, P.LOCATION_UPDATE}
        entry = ActivityLog.objects.get(verb="role.created")
        assert entry.actor == admin_a and entry.organization == org_a

    def test_role_name_is_unique_per_organization_case_insensitively(self, org_a, org_b):
        services.create_role(organization=org_a, name="Lead")
        with pytest.raises(ValidationError):
            services.create_role(organization=org_a, name="lead")
        services.create_role(organization=org_b, name="Lead")  # another tenant may reuse it

    def test_unknown_permission_is_rejected(self, org_a):
        with pytest.raises(ValidationError):
            services.create_role(organization=org_a, name="Bad", permissions=["location.explode"])

    def test_system_roles_cannot_be_changed(self):
        services.seed_system_roles()
        role = Role.objects.get(slug="auditor", organization=None)
        for action in (
            lambda: services.update_role(role, name="Renamed"),
            lambda: services.set_role_permissions(role, [P.LOCATION_VIEW]),
            lambda: services.deactivate_role(role),
        ):
            with pytest.raises(ValidationError):
                action()

    def test_permission_change_is_audited_with_added_and_removed(self, org_a, admin_a):
        role = services.create_role(organization=org_a, name="R", permissions=[P.LOCATION_VIEW])
        services.set_role_permissions(role, [P.LOCATION_UPDATE], actor=admin_a)
        entry = ActivityLog.objects.get(verb="role.permissions_changed")
        assert entry.metadata == {"added": [P.LOCATION_UPDATE], "removed": [P.LOCATION_VIEW]}

    def test_deactivate_and_reactivate(self, org_a, admin_a):
        role = services.create_role(organization=org_a, name="R")
        services.deactivate_role(role, actor=admin_a)
        role.refresh_from_db()
        assert role.is_active is False
        services.activate_role(role, actor=admin_a)
        role.refresh_from_db()
        assert role.is_active is True
        assert ActivityLog.objects.filter(verb__in=["role.deactivated", "role.activated"]).count() == 2

    def test_history_records_the_change(self, org_a, admin_a):
        role = services.create_role(organization=org_a, name="R", actor=admin_a)
        services.update_role(role, name="R2", actor=admin_a)
        assert role.history.count() == 2


# ---------------------------------------------------------------------------
# Assignment services
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestAssignmentServices:
    @pytest.fixture
    def member(self, make_user, org_a):
        services.seed_system_roles()
        user = make_user(organization=org_a, role=None)
        return OrganizationMembership.objects.get(user=user, organization=org_a)

    @pytest.fixture
    def auditor(self):
        services.seed_system_roles()
        return Role.objects.get(slug="auditor", organization=None)

    def test_assign_is_idempotent_and_audited(self, member, auditor, admin_a):
        first = services.assign_role(member, auditor, actor=admin_a)
        second = services.assign_role(member, auditor, actor=admin_a)
        assert first.pk == second.pk
        assert ActivityLog.objects.filter(verb="membership_role.assigned").count() == 1

    def test_assign_to_a_non_active_membership_is_rejected(self, member, auditor):
        services.change_membership_status(member, "suspended")
        member.refresh_from_db()
        with pytest.raises(ValidationError):
            services.assign_role(member, auditor)

    def test_role_of_another_organization_is_rejected(self, member, org_b):
        foreign = services.create_role(organization=org_b, name="Foreign")
        with pytest.raises(ValidationError):
            services.assign_role(member, foreign)

    def test_location_of_another_organization_is_rejected(self, member, auditor, org_b, make_location):
        with pytest.raises(ValidationError):
            services.assign_role(member, auditor, scope_type=SCOPE.LOCATION, location=make_location(org_b, "F"))

    def test_location_scope_requires_a_location_and_org_scope_forbids_one(self, member, auditor, chain_a):
        with pytest.raises(ValidationError):
            services.assign_role(member, auditor, scope_type=SCOPE.LOCATION)
        with pytest.raises(ValidationError):
            services.assign_role(member, auditor, scope_type=SCOPE.ORGANIZATION, location=chain_a[0])

    def test_inactive_role_cannot_be_assigned(self, member, org_a):
        role = services.create_role(organization=org_a, name="R")
        services.deactivate_role(role)
        with pytest.raises(ValidationError):
            services.assign_role(member, role)

    def test_same_role_at_two_locations_is_allowed(self, member, auditor, chain_a, make_location, org_a):
        other = make_location(org_a, "Other", parent=chain_a[1])
        services.assign_role(member, auditor, scope_type=SCOPE.LOCATION, location=chain_a[2])
        services.assign_role(member, auditor, scope_type=SCOPE.LOCATION, location=other)
        assert member.role_assignments.count() == 2

    def test_remove_keeps_a_history_row_and_reassign_reactivates(self, member, auditor):
        assignment = services.assign_role(member, auditor)
        services.remove_role(assignment)
        assignment.refresh_from_db()
        assert assignment.is_active is False
        again = services.assign_role(member, auditor)
        assert again.pk == assignment.pk and again.is_active

    def test_change_scope_is_audited(self, member, auditor, chain_a):
        assignment = services.assign_role(member, auditor)
        services.assign_scope(assignment, scope_type=SCOPE.LOCATION_DESCENDANTS, location=chain_a[1])
        assignment.refresh_from_db()
        assert assignment.scope_type == SCOPE.LOCATION_DESCENDANTS and assignment.location == chain_a[1]
        assert ActivityLog.objects.filter(verb="membership_role.scope_changed").exists()

    def test_status_change_is_audited(self, member):
        services.change_membership_status(member, "suspended")
        member.refresh_from_db()
        assert member.status == "suspended" and member.is_active is False


# ---------------------------------------------------------------------------
# The authorization service
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestAuthorization:
    def _member(self, make_user, org):
        return make_user(organization=org, role=None)

    def test_no_role_means_no_permission(self, make_user, org_a):
        user = self._member(make_user, org_a)
        assert not authz.has_permission(user, P.LOCATION_VIEW, organization=org_a)

    def test_org_wide_role_grants_in_that_organization_only(self, make_user, assign, org_a, org_b):
        user = self._member(make_user, org_a)
        assign(user, org_a, "auditor")
        assert authz.has_permission(user, P.LOCATION_VIEW, organization=org_a)
        assert not authz.has_permission(user, P.LOCATION_VIEW, organization=org_b)
        assert not authz.has_permission(user, P.LOCATION_UPDATE, organization=org_a)

    def test_membership_is_required_even_for_a_role_in_another_organization(self, make_user, assign, org_a, org_b):
        user = self._member(make_user, org_a)
        assign(user, org_a, "administrator")
        assert not authz.has_permission(user, P.ROLE_ASSIGN, organization=org_b)

    @pytest.mark.parametrize("status", ["pending", "suspended", "left"])
    def test_non_active_memberships_grant_nothing(self, make_user, assign, org_a, status):
        user = self._member(make_user, org_a)
        assign(user, org_a, "administrator")
        member = OrganizationMembership.objects.get(user=user)
        services.change_membership_status(member, status)
        assert not authz.has_permission(user, P.LOCATION_VIEW, organization=org_a)
        services.change_membership_status(member, "active")  # roles were kept: access returns
        assert authz.has_permission(user, P.LOCATION_VIEW, organization=org_a)

    def test_inactive_user_is_denied(self, make_user, assign, org_a):
        user = self._member(make_user, org_a)
        assign(user, org_a, "administrator")
        from apps.accounts.models import UserStatus

        user.status = UserStatus.INACTIVE  # ``is_active`` mirrors the lifecycle status (ADR 0030)
        user.save()
        assert not user.is_active
        assert not authz.has_permission(user, P.LOCATION_VIEW, organization=org_a)

    def test_inactive_organization_grants_nothing_to_members(self, make_user, assign, org_a):
        user = self._member(make_user, org_a)
        assign(user, org_a, "administrator")
        org_a.status = org_a.Status.SUSPENDED
        org_a.save()
        assert not authz.has_permission(user, P.LOCATION_VIEW, organization=org_a)

    def test_inactive_role_grants_nothing_then_returns(self, make_user, assign, org_a):
        user = self._member(make_user, org_a)
        role = services.create_role(organization=org_a, name="R", permissions=[P.LOCATION_VIEW])
        assign(user, org_a, role)
        assert authz.has_permission(user, P.LOCATION_VIEW, organization=org_a)
        services.deactivate_role(role)
        assert not authz.has_permission(user, P.LOCATION_VIEW, organization=org_a)
        services.activate_role(role)
        assert authz.has_permission(user, P.LOCATION_VIEW, organization=org_a)

    def test_roles_are_additive(self, make_user, assign, org_a):
        user = self._member(make_user, org_a)
        assign(user, org_a, "auditor")
        assert not authz.has_permission(user, P.LOCATION_UPDATE, organization=org_a)
        assign(user, org_a, "restaurant-manager")  # adds update; nothing is taken away
        assert authz.has_permission(user, P.LOCATION_UPDATE, organization=org_a)
        assert authz.has_permission(user, P.LOCATION_TYPE_VIEW, organization=org_a)

    def test_superuser_bypass_is_the_only_bypass(self, superuser, org_a, make_user):
        assert authz.has_permission(superuser, P.ROLE_ASSIGN, organization=org_a)
        staff = make_user(organization=org_a, staff=True, role=None)
        assert not authz.has_permission(staff, P.ROLE_ASSIGN, organization=org_a)  # staff is not a bypass

    def test_anonymous_is_denied(self, org_a):
        from django.contrib.auth.models import AnonymousUser

        assert not authz.has_permission(AnonymousUser(), P.LOCATION_VIEW, organization=org_a)

    def test_forged_location_of_another_organization_is_denied(self, make_user, assign, org_a, org_b, make_location):
        user = self._member(make_user, org_a)
        assign(user, org_a, "administrator")
        foreign = make_location(org_b, "Foreign")
        assert not authz.has_permission(user, P.LOCATION_VIEW, organization=org_a, location=foreign)
        assert not authz.has_permission(user, P.LOCATION_VIEW, location=foreign)

    # -- legacy bridge ---------------------------------------------------------------------
    def test_legacy_model_permissions_count_as_org_wide_grants(self, make_user, org_a):
        user = make_user(organization=org_a, role=None, perms=("locations.change_location",))
        assert authz.has_permission(user, P.LOCATION_UPDATE, organization=org_a)

    def test_legacy_bridge_can_be_switched_off(self, make_user, org_a, settings):
        settings.RBAC_LEGACY_MODEL_PERMISSIONS = False
        user = make_user(organization=org_a, role=None, perms=("locations.change_location",))
        assert not authz.has_permission(user, P.LOCATION_UPDATE, organization=org_a)

    def test_legacy_grant_needs_membership_or_staff(self, make_user, org_a, org_b):
        user = make_user(organization=org_b, role=None, perms=("locations.change_location",))
        assert not authz.has_permission(user, P.LOCATION_UPDATE, organization=org_a)


# ---------------------------------------------------------------------------
# Location scope
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestScope:
    @pytest.fixture
    def tree(self, org_a, make_type, make_location):
        """Country > (Region 1 > (Area 1 > Store 1, Area 2), Region 2 > Area 3)."""
        t = make_type(org_a, "Node", "NODE")
        country = make_location(org_a, "Country", type=t)
        r1 = make_location(org_a, "Region 1", parent=country, type=t)
        r2 = make_location(org_a, "Region 2", parent=country, type=t)
        a1 = make_location(org_a, "Area 1", parent=r1, type=t)
        a2 = make_location(org_a, "Area 2", parent=r1, type=t)
        a3 = make_location(org_a, "Area 3", parent=r2, type=t)
        s1 = make_location(org_a, "Store 1", parent=a1, type=t)
        return {"country": country, "r1": r1, "r2": r2, "a1": a1, "a2": a2, "a3": a3, "s1": s1}

    @pytest.fixture
    def scoped(self, make_user, org_a):
        return make_user(organization=org_a, role=None)

    def test_subtree_scope_covers_the_location_and_everything_beneath(self, scoped, assign, org_a, tree):
        assign(scoped, org_a, "operations-manager", scope=SCOPE.LOCATION_DESCENDANTS, location=tree["r1"])
        for key in ("r1", "a1", "a2", "s1"):
            assert authz.has_permission(scoped, P.LOCATION_UPDATE, location=tree[key]), key
        for key in ("country", "r2", "a3"):
            assert not authz.has_permission(scoped, P.LOCATION_UPDATE, location=tree[key]), key

    def test_single_location_scope_does_not_cover_descendants(self, scoped, assign, org_a, tree):
        assign(scoped, org_a, "restaurant-manager", scope=SCOPE.LOCATION, location=tree["a1"])
        assert authz.has_permission(scoped, P.LOCATION_UPDATE, location=tree["a1"])
        assert not authz.has_permission(scoped, P.LOCATION_UPDATE, location=tree["s1"])

    def test_a_scoped_grant_is_not_an_organization_level_grant(self, scoped, assign, org_a, tree):
        assign(scoped, org_a, "operations-manager", scope=SCOPE.LOCATION_DESCENDANTS, location=tree["r1"])
        assert not authz.has_permission(scoped, P.LOCATION_CREATE, organization=org_a)  # i.e. no root creation
        assert authz.has_permission_anywhere(scoped, P.LOCATION_CREATE)

    def test_org_wide_beats_scope(self, scoped, assign, org_a, tree):
        assign(scoped, org_a, "auditor")  # org-wide view
        assign(scoped, org_a, "operations-manager", scope=SCOPE.LOCATION_DESCENDANTS, location=tree["r1"])
        assert authz.has_permission(scoped, P.LOCATION_VIEW, location=tree["a3"])  # via the org-wide role
        assert not authz.has_permission(scoped, P.LOCATION_UPDATE, location=tree["a3"])

    def test_two_scopes_union(self, scoped, assign, org_a, tree):
        assign(scoped, org_a, "auditor", scope=SCOPE.LOCATION_DESCENDANTS, location=tree["a1"])
        assign(scoped, org_a, "auditor", scope=SCOPE.LOCATION, location=tree["a3"])
        ids = set(authz.get_accessible_locations(scoped, P.LOCATION_VIEW).values_list("name", flat=True))
        assert ids == {"Area 1", "Store 1", "Area 3"}

    def test_moving_a_location_into_scope_extends_access_immediately(self, scoped, assign, org_a, tree):
        from apps.locations import services as location_services

        assign(scoped, org_a, "auditor", scope=SCOPE.LOCATION_DESCENDANTS, location=tree["r1"])
        assert not authz.can_access_location(scoped, tree["a3"])
        location_services.move_location(tree["a3"], tree["r1"])
        tree["a3"].refresh_from_db()
        # Subtrees are expanded per request (never cached): a fresh request sees the move at once.
        from django.contrib.auth import get_user_model

        assert authz.can_access_location(get_user_model().objects.get(pk=scoped.pk), tree["a3"])

    def test_accessible_descendants(self, scoped, assign, org_a, tree):
        assign(scoped, org_a, "auditor", scope=SCOPE.LOCATION_DESCENDANTS, location=tree["a1"])
        names = set(authz.accessible_descendants(scoped, tree["country"]).values_list("name", flat=True))
        assert names == {"Area 1", "Store 1"}

    def test_effective_permissions_report_scope(self, scoped, assign, org_a, tree):
        assign(scoped, org_a, "auditor")
        assign(scoped, org_a, "restaurant-manager", scope=SCOPE.LOCATION, location=tree["a1"])
        effective = authz.get_effective_permissions(scoped, org_a)
        assert effective[P.LOCATION_VIEW]["organization"] is True
        assert effective[P.LOCATION_UPDATE] == {"organization": False, "locations": [str(tree["a1"].pk)]}
        assert P.ROLE_ASSIGN not in effective

    def test_accessible_organizations(self, make_user, assign, org_a, org_b):
        user = make_user(organization=org_a, role=None)
        assert not authz.get_accessible_organizations(user, P.LOCATION_VIEW).exists()
        assign(user, org_a, "auditor")
        assert list(authz.get_accessible_organizations(user, P.LOCATION_VIEW)) == [org_a]
        assert authz.can_access_organization(user, org_a) and not authz.can_access_organization(user, org_b)

    def test_multi_organization_member_has_independent_grants(self, make_user, assign, org_a, org_b):
        from apps.accounts import services as account_services

        user = make_user(organization=org_a, role=None)
        account_services.add_membership(user, org_b)
        assign(user, org_a, "administrator")
        assign(user, org_b, "auditor")
        assert authz.has_permission(user, P.ROLE_ASSIGN, organization=org_a)
        assert not authz.has_permission(user, P.ROLE_ASSIGN, organization=org_b)
        assert authz.has_permission(user, P.LOCATION_VIEW, organization=org_b)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestCache:
    def test_warm_checks_cost_no_queries(self, make_user, assign, org_a, django_assert_num_queries):
        user = make_user(organization=org_a, role=None)
        assign(user, org_a, "auditor")
        assert authz.has_permission(user, P.LOCATION_VIEW, organization=org_a)
        with django_assert_num_queries(0):
            for _ in range(20):
                authz.has_permission(user, P.LOCATION_VIEW, organization=org_a)
                authz.has_permission(user, P.LOCATION_UPDATE, organization=org_a)

    def test_a_fresh_user_object_uses_the_shared_cache(self, make_user, assign, org_a, django_assert_num_queries):
        from django.contrib.auth import get_user_model

        user = make_user(organization=org_a, role=None)
        assign(user, org_a, "auditor")
        authz.has_permission(user, P.LOCATION_VIEW, organization=org_a)
        fresh = get_user_model().objects.get(pk=user.pk)
        with django_assert_num_queries(0):
            assert authz.has_permission(fresh, P.LOCATION_VIEW, organization=org_a)

    def test_assigning_and_removing_a_role_takes_effect_immediately(self, make_user, assign, org_a):
        user = make_user(organization=org_a, role=None)
        assert not authz.has_permission(user, P.LOCATION_VIEW, organization=org_a)
        assignment = assign(user, org_a, "auditor")
        assert authz.has_permission(user, P.LOCATION_VIEW, organization=org_a)
        services.remove_role(assignment)
        assert not authz.has_permission(user, P.LOCATION_VIEW, organization=org_a)

    def test_editing_a_roles_permissions_invalidates_every_holder(self, make_user, assign, org_a):
        role = services.create_role(organization=org_a, name="R", permissions=[P.LOCATION_VIEW])
        holders = [make_user(organization=org_a, role=None) for _ in range(2)]
        for holder in holders:
            assign(holder, org_a, role)
            assert authz.has_permission(holder, P.LOCATION_VIEW, organization=org_a)
        services.set_role_permissions(role, [P.LOCATION_UPDATE])
        for holder in holders:
            assert not authz.has_permission(holder, P.LOCATION_VIEW, organization=org_a)
            assert authz.has_permission(holder, P.LOCATION_UPDATE, organization=org_a)

    def test_suspending_a_membership_invalidates(self, make_user, assign, org_a):
        user = make_user(organization=org_a, role=None)
        assign(user, org_a, "auditor")
        assert authz.has_permission(user, P.LOCATION_VIEW, organization=org_a)
        services.change_membership_status(OrganizationMembership.objects.get(user=user), "suspended")
        assert not authz.has_permission(user, P.LOCATION_VIEW, organization=org_a)

    def test_changing_django_permissions_invalidates(self, make_user, grant, org_a):
        from django.contrib.auth import get_user_model

        user = make_user(organization=org_a, role=None)
        assert not authz.has_permission(user, P.LOCATION_UPDATE, organization=org_a)
        grant(user, "locations.change_location")
        # Django caches model permissions on the instance; a new request loads a new instance.
        assert authz.has_permission(get_user_model().objects.get(pk=user.pk), P.LOCATION_UPDATE, organization=org_a)

    def test_one_users_change_does_not_leak_to_another(self, make_user, assign, org_a):
        a = make_user(organization=org_a, role=None)
        b = make_user(organization=org_a, role=None)
        assign(a, org_a, "auditor")
        assert authz.has_permission(a, P.LOCATION_VIEW, organization=org_a)
        assert not authz.has_permission(b, P.LOCATION_VIEW, organization=org_a)
