"""Read-only queries for assignments.  Authorization happens *first*: every list starts from ``assignments_visible_to``.

Visible = in an organization the user may enter, and either at a location where they hold ``assignment.view`` (organization-wide
or through a scoped grant, subtrees expanded) or designated as the responsible user of an issued assignment.  Searching,
filtering and pagination are applied on top, so a parameter can never reach another scope.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Case, Count, IntegerField, Q, QuerySet, Value, When
from django.utils import timezone

from apps.core.policies import is_active_user
from apps.core.utils import parse_uuid
from apps.locations.models import Location
from apps.permissions.authorization import get_access, has_permission, visible_organization_ids
from apps.permissions.registry import P

from .constants import DEFAULT_DUE_SOON_HOURS, OPEN_STATUSES, OVERDUE, PRIORITY_RANK, AssignmentStatus
from .models import ChecklistAssignment


def due_soon_hours() -> int:
    return int(getattr(settings, "ASSIGNMENT_DUE_SOON_HOURS", DEFAULT_DUE_SOON_HOURS))


def assignments_visible_to(user: Any, organization: Any = None) -> QuerySet[ChecklistAssignment]:
    """The authorized base queryset (list, detail, filters, API, admin and summaries all start here)."""
    if not is_active_user(user):
        return ChecklistAssignment.objects.none()
    org_ids = visible_organization_ids(user)
    if organization is not None:
        wanted = str(getattr(organization, "pk", organization))
        org_ids = [pk for pk in org_ids if str(pk) == wanted]
    condition = Q()
    for organization_id in org_ids:
        access = get_access(user, organization_id)
        if access.is_org_wide(P.ASSIGNMENT_VIEW):
            condition |= Q(organization_id=organization_id)
        elif access.holds(P.ASSIGNMENT_VIEW):
            condition |= Q(location_id__in=access.location_ids(P.ASSIGNMENT_VIEW))
    if org_ids:  # the designated responsible user sees their own issued assignments
        condition |= Q(assigned_to=user, organization_id__in=org_ids) & ~Q(status=AssignmentStatus.DRAFT)
    if not condition:
        return ChecklistAssignment.objects.none()
    rank = Case(*(When(priority=p, then=Value(r)) for p, r in PRIORITY_RANK.items()), default=Value(0), output_field=IntegerField())
    return (
        ChecklistAssignment.objects.filter(condition)
        .select_related("organization", "version__template", "location__location_type", "assigned_to", "created_by", "issued_by", "cancelled_by")
        .annotate(priority_rank=rank)
    )


def get_assignment(user: Any, pk: Any) -> ChecklistAssignment | None:
    """One visible assignment, or ``None`` (the view answers 404 - existence is not revealed)."""
    pk = parse_uuid(pk)
    if not pk:
        return None
    return assignments_visible_to(user).filter(pk=pk).first()


# -- derived operational views -----------------------------------------------------------------------------------------
def overdue(queryset: QuerySet[ChecklistAssignment], now: Any = None) -> QuerySet[ChecklistAssignment]:
    """Open work past its due date (derived from ``due_at`` and the clock - nothing is persisted)."""
    return queryset.filter(status__in=OPEN_STATUSES, due_at__lt=now or timezone.now())


def due_soon(queryset: QuerySet[ChecklistAssignment], hours: int | None = None, now: Any = None) -> QuerySet[ChecklistAssignment]:
    now = now or timezone.now()
    return queryset.filter(status__in=OPEN_STATUSES, due_at__gte=now, due_at__lte=now + timedelta(hours=hours or due_soon_hours()))


def cancelled(queryset: QuerySet[ChecklistAssignment]) -> QuerySet[ChecklistAssignment]:
    return queryset.filter(status=AssignmentStatus.CANCELLED)


def by_status(queryset: QuerySet[ChecklistAssignment], status: str, now: Any = None) -> QuerySet[ChecklistAssignment]:
    """Filter by a persisted status or by the derived ``overdue``; an open assignment past due counts as overdue, not as open."""
    now = now or timezone.now()
    if status == OVERDUE:
        return overdue(queryset, now)
    if status in OPEN_STATUSES:
        return queryset.filter(status=status).exclude(due_at__lt=now)
    return queryset.filter(status=status)


def for_location(user: Any, location: Any, *, include_descendants: bool = False) -> QuerySet[ChecklistAssignment]:
    queryset = assignments_visible_to(user)
    if not include_descendants:
        return queryset.filter(location=location)
    from apps.locations.selectors import get_descendants

    return queryset.filter(location__in=get_descendants(location, include_self=True).values("pk"))


def for_assignee(user: Any, assignee: Any) -> QuerySet[ChecklistAssignment]:
    return assignments_visible_to(user).filter(assigned_to=assignee)


def for_version(user: Any, version: Any) -> QuerySet[ChecklistAssignment]:
    return assignments_visible_to(user).filter(version=version)


def for_template(user: Any, template: Any) -> QuerySet[ChecklistAssignment]:
    return assignments_visible_to(user).filter(version__template=template)


def summary(queryset: QuerySet[ChecklistAssignment], now: Any = None) -> dict[str, Any]:
    """Counts by (derived) status and by priority: a handful of aggregate queries, whatever the volume."""
    now = now or timezone.now()
    persisted = dict(queryset.values_list("status").annotate(n=Count("pk")))
    late = overdue(queryset, now).count()
    by_status_counts = {s: persisted.get(s, 0) for s, _l in AssignmentStatus.choices}
    open_total = by_status_counts[AssignmentStatus.ASSIGNED] + by_status_counts[AssignmentStatus.IN_PROGRESS]
    by_priority = dict(queryset.values_list("priority").annotate(n=Count("pk")))
    return {
        "total": sum(persisted.values()),
        "by_status": by_status_counts,
        "open": open_total,
        "overdue": late,
        "due_soon": due_soon(queryset, now=now).count(),
        "by_priority": {p: by_priority.get(p, 0) for p in PRIORITY_RANK},
    }


# -- choices for forms (all authorized) --------------------------------------------------------------------------------
def assignable_locations(user: Any, code: str = P.ASSIGNMENT_CREATE, organization: Any = None) -> QuerySet[Location]:
    """Active locations where the user may act with ``code`` (organization-wide or scoped) - never another organization's."""
    from apps.permissions.authorization import get_accessible_locations

    return get_accessible_locations(user, code, organization=organization).filter(is_active=True).select_related("location_type", "parent")


def assignable_versions(user: Any) -> list[Any]:
    """Current published versions of active checklists the user may use, for the "which checklist" choice."""
    from apps.checklists import selectors as checklist_selectors
    from apps.checklists.constants import TemplateStatus, VersionStatus
    from apps.checklists.models import ChecklistVersion

    templates = checklist_selectors.templates_for_user(user).filter(status=TemplateStatus.ACTIVE)
    return list(
        ChecklistVersion.objects.filter(template__in=templates.values("pk"), status=VersionStatus.PUBLISHED)
        .select_related("template").order_by("template__name")
    )


def eligible_assignees(organization: Any, location: Any) -> QuerySet:
    """Users who may be designated at ``location``: active, active members of the organization, and allowed to run checklists there."""
    users = get_user_model().objects.filter(
        is_active=True,
        organization_memberships__organization=organization, organization_memberships__is_active=True,
    ).distinct().order_by("email")
    ids = [u.pk for u in users if has_permission(u, P.EXECUTION_CREATE, organization=organization, location=location)]
    return get_user_model().objects.filter(pk__in=ids).order_by("email")


def expand_targets(user: Any, roots: list[Location], *, include_descendants: bool) -> tuple[list[Location], int]:
    """The final target list for a bulk assignment, and how many descendants were *left out* because they are out of scope.

    Only locations where the user may bulk-assign are ever included; nothing inaccessible is silently selected.
    """
    from apps.locations.selectors import get_descendants

    allowed = assignable_locations(user, P.ASSIGNMENT_BULK_CREATE)
    chosen: dict[Any, Location] = {}
    skipped = 0
    for root in roots:
        candidates = list(get_descendants(root, include_self=True)) if include_descendants else [root]
        reachable = set(allowed.filter(pk__in=[c.pk for c in candidates]).values_list("pk", flat=True))
        for candidate in candidates:
            if candidate.pk in reachable:
                chosen[candidate.pk] = candidate
            elif candidate.pk != root.pk:
                skipped += 1
    return list(chosen.values()), skipped


def timeline(assignment: ChecklistAssignment, limit: int = 50):  # noqa: ANN201
    """The activity entries of one assignment, newest first (the same log every other module writes)."""
    from apps.core.models import ActivityLog

    return (
        ActivityLog.objects.filter(organization_id=assignment.organization_id, metadata__assignment_id=str(assignment.pk))
        .select_related("actor").order_by("-created_at")[:limit]
    )


def location_paths(organization_ids: Any) -> dict[Any, str]:
    """``location id -> "Region / Area / Restaurant"`` for whole organizations in one query (no per-row ancestor lookups)."""
    rows = list(Location.objects.filter(organization_id__in=set(organization_ids)).values_list("pk", "name", "parent_id"))
    by_id = {pk: (name, parent) for pk, name, parent in rows}
    paths: dict[Any, str] = {}
    for pk in by_id:
        parts, current, guard = [], pk, 0
        while current in by_id and guard < 50:
            name, parent = by_id[current]
            parts.append(name)
            current, guard = parent, guard + 1
        paths[pk] = " / ".join(reversed(parts))
    return paths
