"""Authorization policy for the location domain.

Location decisions delegate to the organization policy: a location inherits the
permissions of its owning organization.  This keeps one rule set for the whole
tenant boundary and leaves room for a finer (location-subtree) scope engine in a
later phase without changing these call sites.
"""

from __future__ import annotations

from typing import Any

from apps.organizations.policies import (
    can_manage_organization,
    can_view_organization,
)


def can_view_location(user: Any, location: Any) -> bool:
    """Whether ``user`` may view ``location``."""
    return can_view_organization(user, location.organization)


def can_manage_location(user: Any, location: Any) -> bool:
    """Whether ``user`` may edit / move / (de)activate ``location``."""
    return can_manage_organization(user, location.organization)


def can_manage_locations(user: Any, organization: Any) -> bool:
    """Whether ``user`` may manage the locations of an organization."""
    return can_manage_organization(user, organization)


def can_view_location_types(user: Any, organization: Any) -> bool:
    """Whether ``user`` may see the location types of an organization."""
    return can_view_organization(user, organization)


def can_manage_location_types(user: Any, organization: Any) -> bool:
    """Whether ``user`` may manage the location types of an organization."""
    return can_manage_organization(user, organization)
