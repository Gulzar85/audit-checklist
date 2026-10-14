"""Read-only query helpers for organizations."""

from __future__ import annotations

from typing import Any

from django.db.models import Q, QuerySet

from apps.core.policies import is_platform_staff

from .models import Organization, OrganizationMembership


def organizations_visible_to(user: Any) -> QuerySet[Organization]:
    """Return every organization ``user`` may see.

    Platform staff see all organizations; everyone else sees only the
    organizations they are an active member of.  Anonymous/inactive users see
    nothing.  This is the single source of truth for organization-level read
    scoping (web and API both use it).
    """
    queryset = Organization.objects.all()
    if is_platform_staff(user):
        return queryset
    if not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return queryset.none()
    return queryset.filter(
        memberships__user=user,
        memberships__is_active=True,
    ).distinct()


def active_organizations() -> QuerySet[Organization]:
    """All organizations in the active lifecycle state."""
    return Organization.objects.active()


def organizations_for_user(user: Any) -> QuerySet[Organization]:
    """Active organizations visible to ``user`` (convenience for pickers)."""
    return organizations_visible_to(user).filter(status=Organization.Status.ACTIVE)


def memberships_for_user(user: Any) -> QuerySet[OrganizationMembership]:
    """Active memberships for a user, with the organization joined."""
    return (
        OrganizationMembership.objects.filter(user=user, is_active=True)
        .select_related("organization")
        .order_by("organization__name")
    )


def search_organizations(user: Any, *, term: str = "") -> QuerySet[Organization]:
    """Visible organizations filtered by a free-text term (name/code/legal name)."""
    queryset = organizations_visible_to(user)
    if term:
        queryset = queryset.filter(
            Q(name__icontains=term)
            | Q(code__icontains=term)
            | Q(legal_name__icontains=term)
        )
    return queryset
