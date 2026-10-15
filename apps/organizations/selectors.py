"""Read-only query helpers for organizations.

``organizations_visible_to`` is the single source of truth for organization-level
read scoping.  Location, location-type and (later) every other tenant-owned
selector builds on it, and the HTML views and the API both start from it - so
there is no separate "API security model".
"""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.core.policies import is_active_user, is_platform_staff

from .models import Organization, OrganizationMembership


def organizations_visible_to(user: Any) -> QuerySet[Organization]:
    """Return every organization ``user`` may see.

    * Anonymous / inactive users see nothing.
    * Platform staff see every organization, whatever its state.
    * Everyone else sees only organizations they are an *active member* of **and**
      that are themselves ``active``.  A member of an inactive or suspended
      organization therefore gets "not found" for everything inside it: that is
      how "inactive organizations cannot perform normal operations" is enforced
      uniformly across the UI and the API (ADR 0011).
    """
    if not is_active_user(user):
        return Organization.objects.none()
    if is_platform_staff(user):
        return Organization.objects.all()
    member_of = OrganizationMembership.objects.filter(user=user, is_active=True).values(
        "organization_id"
    )
    return Organization.objects.filter(status=Organization.Status.ACTIVE, pk__in=member_of)
