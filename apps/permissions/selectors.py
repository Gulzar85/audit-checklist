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


# ---------------------------------------------------------------------------
# Roles, members and the permission matrix (Phase 3)
# ---------------------------------------------------------------------------
def roles_for_display(organization: Any) -> QuerySet[Role]:
    """Roles shown in an organization: its own custom roles plus the platform (system) roles."""
    from django.db.models import Count

    org_id = getattr(organization, "pk", organization)
    return (
        Role.objects.filter(Q(organization_id=org_id) | Q(organization__isnull=True))
        .annotate(
            permission_count=Count("role_permissions", filter=Q(role_permissions__permission__is_active=True), distinct=True),
            member_count=Count(
                "membership_assignments",
                filter=Q(
                    membership_assignments__is_active=True,
                    membership_assignments__membership__organization_id=org_id,
                    membership_assignments__membership__is_active=True,
                ),
                distinct=True,
            ),
        )
        .order_by("-is_system", "name")
    )


def members_for_display(organization: Any) -> QuerySet:
    """Memberships of an organization with the data the members screen needs (no N+1)."""
    from django.db.models import Prefetch

    from apps.organizations.models import OrganizationMembership

    from .models import MembershipRole

    return (
        OrganizationMembership.objects.filter(organization=organization)
        .select_related("user")
        .prefetch_related(
            Prefetch(
                "role_assignments",
                queryset=MembershipRole.objects.filter(is_active=True, role__is_active=True).select_related(
                    "role", "location"
                ),
                to_attr="active_assignments",
            )
        )
        .order_by("user__display_name", "user__email")
    )


def permission_matrix(selected: set[str] | None = None) -> dict[str, Any]:
    """The registry shaped for the role editor: ``columns`` (actions) x ``rows`` (resources).

    Each row is ``{"resource", "label", "cells": [cell | None, ...]}`` aligned to ``columns``; a
    cell is ``{"code", "label", "checked", "org_wide_only"}``.  ``None`` means "this resource has
    no such action", so every row has the same number of columns and the table stays rectangular.
    """
    from . import registry

    selected = selected or set()
    actions: list[str] = []
    for definition in registry.REGISTRY:
        if definition.action not in actions:
            actions.append(definition.action)
    rows: dict[str, dict[str, Any]] = {}
    for definition in registry.REGISTRY:
        row = rows.setdefault(
            definition.resource,
            {"resource": definition.resource,
             "label": registry.RESOURCE_LABELS.get(definition.resource, definition.resource.title()),
             "by_action": {}},
        )
        row["by_action"][definition.action] = {
            "code": definition.code,
            "label": definition.label,
            "checked": definition.code in selected,
            "org_wide_only": definition.org_wide_only,
        }
    result_rows = []
    for row in rows.values():
        cells = [row["by_action"].get(action) for action in actions]
        result_rows.append({"resource": row["resource"], "label": row["label"], "cells": cells,
                            "all_checked": all(c["checked"] for c in cells if c)})
    return {"columns": actions, "rows": result_rows}


def location_path(location: Any, *, ancestors_by_id: dict | None = None) -> str:
    """``Country 1 › Region 2 › Area 3`` for a location (breadcrumb text, root first)."""
    names = [location.name]
    seen = {location.pk}
    parent_id = location.parent_id
    cache = ancestors_by_id or {}
    while parent_id and parent_id not in seen:
        parent = cache.get(parent_id)
        if parent is None:
            from apps.locations.models import Location

            parent = Location.objects.filter(pk=parent_id).only("name", "parent_id").first()
            if parent is None:
                break
        names.append(parent.name)
        seen.add(parent_id)
        parent_id = parent.parent_id
    return " › ".join(reversed(names))


def describe_assignment(assignment: Any, *, path_cache: dict | None = None) -> dict[str, str]:
    """Human text for one assignment: ``{"scope": "...", "path": "..."}``."""
    from .models import MembershipRole

    if assignment.scope_type == MembershipRole.ScopeType.ORGANIZATION or assignment.location is None:
        return {"scope": "Entire organization", "path": ""}
    suffix = " and everything beneath it" if assignment.scope_type == MembershipRole.ScopeType.LOCATION_DESCENDANTS else ""
    return {
        "scope": f"{assignment.location.name}{suffix}",
        "path": location_path(assignment.location, ancestors_by_id=path_cache),
    }


def access_summary(membership: Any) -> dict[str, Any]:
    """What a member can do, for the access summary card (roles, scopes, permission count)."""
    from .authorization import get_effective_permissions

    assignments = list(
        membership.role_assignments.filter(is_active=True, role__is_active=True).select_related("role", "location")
    )
    effective = get_effective_permissions(membership.user, membership.organization)
    organization_wide = [code for code, grant in effective.items() if grant["organization"]]
    return {
        "roles": sorted({a.role.name for a in assignments}),
        "assignment_count": len(assignments),
        "permission_count": len(effective),
        "organization_wide_count": len(organization_wide),
        "scoped_count": len(effective) - len(organization_wide),
        "grants_access": membership.is_active and bool(effective),
    }