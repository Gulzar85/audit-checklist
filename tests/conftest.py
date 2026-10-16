"""Shared pytest fixtures."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission

from apps.locations import services as location_services
from apps.locations.models import Location, LocationType
from apps.organizations.models import Organization, OrganizationMembership
from apps.permissions.models import Role, UserRole

User = get_user_model()

#: Every Phase 1 model permission, used to build an "organization admin".
MANAGE_PERMS = (
    "organizations.view_organization",
    "organizations.change_organization",
    "locations.view_location",
    "locations.add_location",
    "locations.change_location",
    "locations.view_locationtype",
    "locations.add_locationtype",
    "locations.change_locationtype",
)


@pytest.fixture(autouse=True)
def _clear_authorization_cache():
    """Grants are cached; a test must never see another test's cache."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def user(db) -> "User":
    return User.objects.create_user(
        email="auditor@example.com",
        password="Str0ng-Passw0rd!",
        display_name="Audrey Auditor",
        first_name="Audrey",
        last_name="Auditor",
    )


@pytest.fixture
def superuser(db) -> "User":
    return User.objects.create_superuser(
        email="admin@example.com",
        password="Adm1n-Passw0rd!",
        display_name="Ada Admin",
    )


@pytest.fixture
def organization(db) -> Organization:
    return Organization.objects.create(name="Acme Corp", slug="acme")


@pytest.fixture
def membership(db, user, organization) -> OrganizationMembership:
    return OrganizationMembership.objects.create(
        user=user, organization=organization, is_default=True
    )


@pytest.fixture
def auditor_role(db) -> Role:
    return Role.objects.create(name="Auditor", slug="auditor")


@pytest.fixture
def granted_role(db, user, auditor_role) -> UserRole:
    return UserRole.objects.create(user=user, role=auditor_role)


@pytest.fixture
def authenticated_client(client, user):
    client.force_login(user)
    return client


# ---------------------------------------------------------------------------
# Organization / location building blocks
# ---------------------------------------------------------------------------
@pytest.fixture
def grant():
    """``grant(user, "app.codename", ...)`` adds model permissions to a user."""

    def _grant(target, *perms: str):
        for perm in perms:
            app_label, codename = perm.split(".")
            target.user_permissions.add(
                Permission.objects.get(content_type__app_label=app_label, codename=codename)
            )
        return target

    return _grant


@pytest.fixture
def make_user(db, grant):
    """Create a user, optionally a member of ``organization`` with ``perms``."""
    counter = {"n": 0}

    def _make(*, email=None, organization=None, perms=(), staff=False, active=True, member_active=True,
              role="report-viewer"):
        """``role`` is the system role given org-wide to a member (a plain member can read, as
        before RBAC); pass ``role=None`` for a member who holds *nothing* (default-deny tests)."""
        counter["n"] += 1
        account = User.objects.create_user(
            email=email or f"user{counter['n']}@example.com",
            password="Str0ng-Passw0rd!",
            is_staff=staff,
            is_active=active,
        )
        if organization is not None:
            member = OrganizationMembership.objects.create(
                user=account, organization=organization, is_active=member_active
            )
            if role is not None:
                from apps.permissions import services as rbac
                from apps.permissions.models import MembershipRole

                rbac.seed_system_roles()
                # Direct ORM: fixtures must not write audit rows that other tests then see.
                MembershipRole.objects.create(membership=member, role=Role.objects.get(organization=None, slug=role))
        grant(account, *perms)
        return account

    return _make


@pytest.fixture
def assign(db):
    """``assign(user, org, "role-slug", scope="organization"|"location"|"location_descendants", location=None)``.

    Goes through the service (validation, audit) and returns the ``MembershipRole``.  Roles are
    the seeded system roles unless ``role`` is a ``Role`` instance.
    """
    from apps.permissions import services as rbac
    from apps.permissions.models import MembershipRole

    def _assign(target, organization, role, *, scope=MembershipRole.ScopeType.ORGANIZATION, location=None, actor=None):
        rbac.seed_system_roles()
        if not isinstance(role, Role):
            role = Role.objects.get(organization=None, slug=role)
        membership = OrganizationMembership.objects.get(user=target, organization=organization)
        return rbac.assign_role(membership, role, scope_type=scope, location=location, actor=actor)

    return _assign


@pytest.fixture
def org_a(db) -> Organization:
    return Organization.objects.create(name="Org A", slug="org-a")


@pytest.fixture
def org_b(db) -> Organization:
    return Organization.objects.create(name="Org B", slug="org-b")


@pytest.fixture
def admin_a(make_user, org_a):
    """A member of Org A who holds every Phase 1 permission (an 'org admin')."""
    return make_user(email="admin-a@example.com", organization=org_a, perms=MANAGE_PERMS)


@pytest.fixture
def admin_b(make_user, org_b):
    return make_user(email="admin-b@example.com", organization=org_b, perms=MANAGE_PERMS)


@pytest.fixture
def viewer_a(make_user, org_a):
    """A member of Org A with *no* model permissions (read-only)."""
    return make_user(email="viewer-a@example.com", organization=org_a)


@pytest.fixture
def staff_user(make_user):
    """Platform staff holding every Phase 1 permission (plus add_organization)."""
    return make_user(
        email="staff@example.com",
        staff=True,
        perms=(*MANAGE_PERMS, "organizations.add_organization"),
    )


#: Everything a theme administrator may do (design, publish, activate).
THEME_PERMS = (
    "themes.view_theme",
    "themes.add_theme",
    "themes.change_theme",
    "themes.publish_theme",
    "themes.activate_theme",
)


@pytest.fixture
def theme_admin_a(make_user, org_a):
    """A member of Org A who may design, publish and activate themes."""
    return make_user(email="theme-admin-a@example.com", organization=org_a, perms=THEME_PERMS)


@pytest.fixture
def theme_admin_b(make_user, org_b):
    return make_user(email="theme-admin-b@example.com", organization=org_b, perms=THEME_PERMS)


@pytest.fixture
def theme_staff(make_user):
    """Platform staff who may manage system themes."""
    return make_user(email="theme-staff@example.com", staff=True, perms=THEME_PERMS)


@pytest.fixture
def make_theme(db):
    """``make_theme(org, name, status="draft", **tokens)`` - built through the services."""
    from apps.themes import services as theme_services
    from apps.themes.constants import ThemeStatus

    def _make(organization=None, name="Test theme", *, status=ThemeStatus.DRAFT, tokens=None, actor=None, **extra):
        theme = theme_services.create_theme(
            actor=actor, organization=organization, name=name, tokens=tokens, **extra
        )
        if status in (ThemeStatus.PUBLISHED, ThemeStatus.ACTIVE):
            theme = theme_services.publish_theme(theme, actor=actor)
        if status == ThemeStatus.ACTIVE:
            theme = theme_services.activate_theme(theme, actor=actor)
        if status == ThemeStatus.ARCHIVED:
            theme = theme_services.archive_theme(theme, actor=actor)
        return theme

    return _make


@pytest.fixture
def make_type(db):
    counter = {"n": 0}

    def _make(organization, name=None, code=None, **extra) -> LocationType:
        counter["n"] += 1
        return LocationType.objects.create(
            organization=organization,
            name=name or f"Type {counter['n']}",
            code=(code or f"T{counter['n']}").upper(),  # factories skip normalisation
            **extra,
        )

    return _make


@pytest.fixture
def make_location(db, make_type):
    """``make_location(org, "Name", parent=None, type=None)`` - bypasses services."""
    counter = {"n": 0}
    default_types: dict = {}

    def _make(organization, name=None, *, parent=None, type=None, code=None, **extra) -> Location:
        counter["n"] += 1
        if type is None:
            if organization.pk not in default_types:
                default_types[organization.pk] = make_type(organization, "Node", "NODE")
            type = default_types[organization.pk]
        return Location.objects.create(
            organization=organization,
            parent=parent,
            location_type=type,
            name=name or f"Location {counter['n']}",
            code=(code or f"LOC{counter['n']}").upper(),  # factories skip normalisation
            **extra,
        )

    return _make


@pytest.fixture
def chain_a(org_a, make_type, make_location):
    """Org A: Country > Region > Area > Branch > Store, five levels deep."""
    names = ["Country", "Region", "Area", "Branch", "Store"]
    types = [make_type(org_a, name, name.upper()) for name in names]
    nodes: list[Location] = []
    parent = None
    for type_, name in zip(types, names, strict=True):
        parent = make_location(org_a, f"{name} 1", parent=parent, type=type_, code=f"{name.upper()}-1")
        nodes.append(parent)
    return nodes


@pytest.fixture
def services():
    return location_services
