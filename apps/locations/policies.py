"""Authorization policy for the location domain.

Locations inherit the tenant boundary from their organization (you must be able to *enter* the
organization) and are then governed by RBAC (Phase 3, ADR 0043): each action asks
:func:`apps.permissions.authorization.has_permission` for the matching permission **at the
location** being acted on.  A grant held organization-wide covers every location; a grant
scoped to a location (or subtree) covers only that part of the hierarchy.  So:

* *view / edit / deactivate* need the permission at that location;
* *create* needs it at the **parent** (a root location needs it organization-wide);
* *move* needs ``location.move`` at the location **and** ``location.create``-level standing at
  the destination, so a scoped manager cannot move a location out of - or into - someone
  else's subtree.

Platform-defined location types (no organization) are managed by platform staff only
(ADR 0016); organization admins can use them but not edit them.  Location *types* are
reference data: any member may read them, writing them needs the type permissions
organization-wide.
"""

from __future__ import annotations

from typing import Any

from apps.core.policies import is_active_user, is_platform_staff
from apps.organizations.policies import can_view_organization
from apps.permissions.authorization import has_permission, has_permission_anywhere
from apps.permissions.registry import P


def _in_scope(user: Any, organization_id: Any, location: Any = None) -> bool:  # noqa: ARG001
    """Whether ``user`` may enter this organization (layer 2); roles are checked separately."""
    return can_view_organization(user, organization_id)


# --- Locations ---------------------------------------------------------------
def can_view_location(user: Any, location: Any) -> bool:
    return has_permission(user, P.LOCATION_VIEW, location=location)


def can_create_location(user: Any, organization: Any = None, parent: Any = None) -> bool:
    """May ``user`` create a location - under ``parent``, or as a root of ``organization``?

    With neither, answers "could this user create locations *somewhere*" (to offer the button).
    """
    if parent is not None:
        return has_permission(user, P.LOCATION_CREATE, location=parent)
    if organization is None:
        return has_permission_anywhere(user, P.LOCATION_CREATE)
    return has_permission(user, P.LOCATION_CREATE, organization=organization)


def can_create_location_in_organization(user: Any, organization: Any) -> bool:
    """Whether ``user`` may create locations *somewhere* in ``organization`` (any scope).

    A manager scoped to one region may create beneath it; the concrete parent is then checked by
    :func:`can_create_location`.
    """
    from apps.permissions.authorization import get_access

    if not is_active_user(user):
        return False
    return get_access(user, organization).holds(P.LOCATION_CREATE)


def can_edit_location(user: Any, location: Any) -> bool:
    return has_permission(user, P.LOCATION_UPDATE, location=location)


def can_move_location(user: Any, location: Any, new_parent: Any = None) -> bool:
    """Moving needs rights over the location *and* the destination subtree."""
    if not has_permission(user, P.LOCATION_MOVE, location=location):
        return False
    if new_parent is None:
        return has_permission(user, P.LOCATION_MOVE, organization=location.organization_id)
    return has_permission(user, P.LOCATION_MOVE, location=new_parent)


def can_deactivate_location(user: Any, location: Any) -> bool:
    return has_permission(user, P.LOCATION_DEACTIVATE, location=location)


def can_activate_location(user: Any, location: Any) -> bool:
    return has_permission(user, P.LOCATION_DEACTIVATE, location=location)


# --- Location types ----------------------------------------------------------
def _can_manage_types(user: Any, organization_id: Any, code: str, legacy_perm: str) -> bool:
    if not is_active_user(user):
        return False
    if organization_id is None:  # a platform-defined type
        return is_platform_staff(user) and user.has_perm(legacy_perm)
    return has_permission(user, code, organization=organization_id)


def can_create_location_type(user: Any, organization: Any = None) -> bool:
    """``organization=None`` asks about a platform-defined type."""
    return _can_manage_types(
        user, getattr(organization, "pk", organization), P.LOCATION_TYPE_CREATE, "locations.add_locationtype"
    )


def can_create_any_location_type(user: Any) -> bool:
    """May ``user`` create a location type for *some* organization (or platform-wide)?"""
    if not is_active_user(user):
        return False
    if is_platform_staff(user) and user.has_perm("locations.add_locationtype"):
        return True
    return has_permission_anywhere(user, P.LOCATION_TYPE_CREATE)


def can_edit_location_type(user: Any, location_type: Any) -> bool:
    return _can_manage_types(
        user, location_type.organization_id, P.LOCATION_TYPE_UPDATE, "locations.change_locationtype"
    )


def can_toggle_location_type(user: Any, location_type: Any) -> bool:
    return can_edit_location_type(user, location_type)
