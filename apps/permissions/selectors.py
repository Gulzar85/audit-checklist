"""Read-only query helpers for permissions."""

from __future__ import annotations

from typing import Any

from django.db.models import Q, QuerySet
from django.utils import timezone

from .models import Role, UserRole


def active_role_slugs_for_user(user: Any, *, organization: Any = None) -> set[str]:
    """Return the set of active role slugs granted to a user.

    A role assignment is active when it is flagged active, has not expired and
    its role is active.  When ``organization`` is supplied the result is
    narrowed to roles explicitly scoped to that organization plus platform-wide
    roles (which have no organization).
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return set()

    now = timezone.now()
    qs = UserRole.objects.filter(
        user=user,
        is_active=True,
        role__is_active=True,
    ).filter(Q(expires_at__isnull=True) | Q(expires_at__gte=now))

    if organization is not None:
        org_id = getattr(organization, "pk", organization)
        qs = qs.filter(
            Q(role__organization_id=org_id) | Q(role__organization__isnull=True)
        )

    return set(qs.values_list("role__slug", flat=True).distinct())


def roles_for_organization(organization_id: Any) -> QuerySet[Role]:
    """Return active roles available to an organization (incl. platform roles)."""
    return Role.objects.filter(
        Q(organization_id=organization_id) | Q(organization__isnull=True),
        is_active=True,
    )
