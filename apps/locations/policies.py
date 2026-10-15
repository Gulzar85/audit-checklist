"""Authorization policy for the location domain.

Locations and location types inherit the tenant boundary from their
organization: a user can act on a location only if they can *see* its
organization (members of active organizations; platform staff everywhere) **and**
hold the relevant Django model permission.

Location-scoped access (Phase 4 - "User A may only touch Region 1's subtree")
refines :func:`_in_scope` below.  Every call site already passes the *location*
being acted on (and, for moves, the destination), which is all a subtree rule
needs, so nothing outside this module has to change.

Platform-defined location types (no organization) are managed by platform staff
only (ADR 0016); organization admins can use them but not edit them.
"""

from __future__ import annotations

from typing import Any

from apps.core.policies import is_active_user, is_platform_staff
from apps.organizations.policies import can_view_organization
from apps.organizations.selectors import organizations_visible_to


def _in_scope(user: Any, organization_id: Any, location: Any = None) -> bool:  # noqa: ARG001
    """Whether the user's scope covers this organization (and, later, location)."""
    return can_view_organization(user, organization_id)


# --- Locations ---------------------------------------------------------------
def can_view_location(user: Any, location: Any) -> bool:
    return _in_scope(user, location.organization_id, location)


def can_create_location(user: Any, organization: Any = None) -> bool:
    """May ``user`` create locations - in ``organization``, or anywhere if omitted."""
    if not is_active_user(user) or not user.has_perm("locations.add_location"):
        return False
    if organization is None:
        return organizations_visible_to(user).exists()
    return _in_scope(user, getattr(organization, "pk", organization))


def can_edit_location(user: Any, location: Any) -> bool:
    return (
        is_active_user(user)
        and user.has_perm("locations.change_location")
        and _in_scope(user, location.organization_id, location)
    )


def can_move_location(user: Any, location: Any, new_parent: Any = None) -> bool:
    """Moving needs rights over the location *and* the destination subtree."""
    if not can_edit_location(user, location):
        return False
    return new_parent is None or _in_scope(user, new_parent.organization_id, new_parent)


def can_deactivate_location(user: Any, location: Any) -> bool:
    return can_edit_location(user, location)


def can_activate_location(user: Any, location: Any) -> bool:
    return can_edit_location(user, location)


# --- Location types ----------------------------------------------------------
def _can_manage_types(user: Any, organization_id: Any, perm: str) -> bool:
    if not is_active_user(user) or not user.has_perm(perm):
        return False
    if organization_id is None:  # a platform-defined type
        return is_platform_staff(user)
    return _in_scope(user, organization_id)


def can_create_location_type(user: Any, organization: Any = None) -> bool:
    """``organization=None`` asks about a platform-defined type."""
    return _can_manage_types(
        user, getattr(organization, "pk", organization), "locations.add_locationtype"
    )


def can_create_any_location_type(user: Any) -> bool:
    """May ``user`` create a location type for *some* organization (or platform-wide)?"""
    if not is_active_user(user) or not user.has_perm("locations.add_locationtype"):
        return False
    return is_platform_staff(user) or organizations_visible_to(user).exists()


def can_edit_location_type(user: Any, location_type: Any) -> bool:
    return _can_manage_types(user, location_type.organization_id, "locations.change_locationtype")


def can_toggle_location_type(user: Any, location_type: Any) -> bool:
    return can_edit_location_type(user, location_type)
