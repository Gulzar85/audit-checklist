"""Authorization policy for roles, permissions and access assignment.

A policy answers *"may this user do this to this object?"* and nothing else.  The HTML views,
the API and the admin all call these functions (ADR 0012); nobody branches on a role name.

Rule set (ADR 0043):

* **View roles / the permission catalogue** - hold ``role.view`` / ``permission.view`` in the
  organization.  System roles (platform templates) are visible to anyone who may see roles.
* **Create / edit / deactivate a custom role** - hold the matching permission organization-wide
  *and* hold every permission the role carries (nobody can mint a role more powerful than
  themselves).  System roles are never editable here.
* **Assign a role** - hold ``role.assign`` at the scope being granted, hold every permission of
  the role at that scope, never to yourself, and only a superuser may hand out a role holding
  permissions the platform reserves (the Super Administrator role).
* **See a member's access** - ``user.view`` or ``role.assign`` in the organization.

"Hold at that scope" means :func:`apps.permissions.authorization.has_permission` with the
location in question, so a manager scoped to one region can grant access inside that region
and nowhere else (no scope escalation).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from apps.core.policies import is_active_user

from . import registry
from .authorization import get_access, has_permission
from .models import MembershipRole, Role

P = registry.P


def _org_id(organization: Any) -> Any:
    return getattr(organization, "pk", organization)


# --- permission catalogue / roles ---------------------------------------------------------
def can_view_permissions(user: Any, organization: Any) -> bool:
    return has_permission(user, P.PERMISSION_VIEW, organization=organization)


def can_view_roles(user: Any, organization: Any) -> bool:
    return has_permission(user, P.ROLE_VIEW, organization=organization)


def can_view_role(user: Any, role: Role, organization: Any) -> bool:
    """A role is visible in ``organization`` if it is a platform template or belongs to it."""
    if role.organization_id not in (None, _org_id(organization)):
        return False
    return can_view_roles(user, organization)


def holds_all(user: Any, codes: Iterable[str], *, organization: Any, location: Any = None) -> bool:
    """Whether ``user`` holds every code at that scope (the anti-escalation test)."""
    if getattr(user, "is_superuser", False):
        return True
    return all(has_permission(user, code, organization=organization, location=location) for code in codes)


def role_permission_codes(role: Role) -> list[str]:
    return list(role.role_permissions.filter(permission__is_active=True).values_list("permission__code", flat=True))


def can_create_role(user: Any, organization: Any, codes: Iterable[str] = ()) -> bool:
    return has_permission(user, P.ROLE_CREATE, organization=organization) and holds_all(
        user, codes, organization=organization
    )


def can_edit_role(user: Any, role: Role, codes: Iterable[str] | None = None) -> bool:
    """Edit a custom role (details and permissions)."""
    if role.is_system or role.organization_id is None:
        return False
    if not has_permission(user, P.ROLE_UPDATE, organization=role.organization_id):
        return False
    # You may only keep or grant permissions you hold yourself.
    wanted = set(codes if codes is not None else ()) | set(role_permission_codes(role))
    return holds_all(user, wanted, organization=role.organization_id)


def can_deactivate_role(user: Any, role: Role) -> bool:
    if role.is_system or role.organization_id is None:
        return False
    return has_permission(user, P.ROLE_DEACTIVATE, organization=role.organization_id)


# --- assignment ---------------------------------------------------------------------------
def can_view_access(user: Any, membership: Any) -> bool:
    """See a member's roles and scope."""
    if not is_active_user(user):
        return False
    access = get_access(user, membership.organization_id)
    return access.holds(P.USER_VIEW) or access.holds(P.ROLE_ASSIGN)


def can_assign_role(
    actor: Any,
    membership: Any,
    role: Role,
    *,
    scope_type: str = MembershipRole.ScopeType.ORGANIZATION,
    location: Any = None,
) -> bool:
    """May ``actor`` grant ``role`` to ``membership`` at that scope?"""
    if not is_active_user(actor):
        return False
    if actor.pk == membership.user_id and not actor.is_superuser:
        return False  # nobody grants themselves access
    organization_id = membership.organization_id
    if role.organization_id not in (None, organization_id):
        return False
    scoped = scope_type != MembershipRole.ScopeType.ORGANIZATION
    if scoped and (location is None or location.organization_id != organization_id):
        return False
    target = location if scoped else None
    if not has_permission(actor, P.ROLE_ASSIGN, organization=organization_id, location=target):
        return False
    codes = role_permission_codes(role)
    if role.slug == "super-administrator" and role.is_system and not actor.is_superuser:
        return False
    return holds_all(actor, codes, organization=organization_id, location=target)


def can_remove_role(actor: Any, assignment: MembershipRole) -> bool:
    """Removing a grant needs the same standing as making it (and never on yourself)."""
    membership = assignment.membership
    if not is_active_user(actor) or (actor.pk == membership.user_id and not actor.is_superuser):
        return False
    target = assignment.location if assignment.scope_type != MembershipRole.ScopeType.ORGANIZATION else None
    return has_permission(actor, P.ROLE_ASSIGN, organization=membership.organization_id, location=target)


def can_manage_membership_status(actor: Any, membership: Any) -> bool:
    if not is_active_user(actor) or (actor.pk == membership.user_id and not actor.is_superuser):
        return False
    return has_permission(actor, P.MEMBERSHIP_MANAGE, organization=membership.organization_id)


def assignable_codes(actor: Any, organization: Any, location: Any = None) -> set[str]:
    """Permission codes ``actor`` could hand out at that scope (used to filter role choices)."""
    if actor.is_superuser:
        return set(registry.codes())
    access = get_access(actor, organization)
    if location is None:
        return set(access.org_wide)
    return {code for code in access.codes if access.allows_location(code, location.pk)}


# --- Object-level, import and export boundaries ----------------------------------------------
def can_access_object(user: Any, obj: Any, code: str, *, guardian_perm: str | None = None) -> bool:
    """Object-level check: RBAC at the object's location/organization, plus optional guardian grants.

    The organization boundary is never delegated to django-guardian: an explicit per-object guardian
    grant (``guardian_perm``, e.g. ``"change_theme"``) only *adds* access inside an organization the
    user may already enter.  Objects with neither a location nor an organization are denied.
    """
    organization_id = getattr(obj, "organization_id", None)
    location = obj if hasattr(obj, "parent_id") else getattr(obj, "location", None)
    if organization_id is None and location is None:
        return False
    if has_permission(user, code, organization=organization_id, location=location):
        return True
    if guardian_perm and is_active_user(user):
        from .authorization import can_access_organization

        org = organization_id or location.organization_id
        return can_access_organization(user, org) and user.has_perm(guardian_perm, obj)
    return False


def can_import_into(user: Any, organization: Any, code: str, location: Any = None) -> bool:
    """May ``user`` import records (needing ``code``, e.g. ``location.create``) into this organization/location?

    Importers must call this for every target row's organization and location.
    """
    if location is not None and location.organization_id != _org_id(organization):
        return False
    return has_permission(user, code, organization=organization, location=location)


def exportable_locations(user: Any, code: str = P.LOCATION_VIEW, *, organization: Any = None):  # noqa: ANN201
    """The only queryset an export may start from: everything the user may see, nothing else."""
    from .authorization import get_accessible_locations

    return get_accessible_locations(user, code, organization=organization)