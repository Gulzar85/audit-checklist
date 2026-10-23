"""Read-only queries for findings.  Authorization happens *first*: every list starts from ``findings_visible_to``.

Visible = in an organization the user may enter, and either at a location where they hold ``finding.view`` (organization-wide or
through a scoped grant, subtrees expanded) or designated as the responsible user.  Filtering, searching and pagination are applied
on top, so a parameter can never reach another scope.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth import get_user_model
from django.db.models import Count, Q, QuerySet
from django.utils import timezone

from apps.core.policies import is_active_user
from apps.core.utils import parse_uuid
from apps.permissions.authorization import get_access, get_accessible_locations, has_permission, visible_organization_ids
from apps.permissions.registry import P

from .constants import ACTIVE_STATUSES, AWAITING_CLOSURE, FindingStatus
from .models import Finding, FindingCategory, FindingSeverity


def findings_visible_to(user: Any, organization: Any = None) -> QuerySet[Finding]:
    """The authorized base queryset (list, detail, filters, API, admin and summaries all start here)."""
    if not is_active_user(user):
        return Finding.objects.none()
    org_ids = visible_organization_ids(user)
    if organization is not None:
        wanted = str(getattr(organization, "pk", organization))
        org_ids = [pk for pk in org_ids if str(pk) == wanted]
    condition = Q()
    for organization_id in org_ids:
        access = get_access(user, organization_id)
        if access.is_org_wide(P.FINDING_VIEW):
            condition |= Q(organization_id=organization_id)
        elif access.holds(P.FINDING_VIEW):
            condition |= Q(location_id__in=access.location_ids(P.FINDING_VIEW))
    if org_ids:
        condition |= Q(assigned_to=user, organization_id__in=org_ids)
    if not condition:
        return Finding.objects.none()
    return (Finding.objects.filter(condition)
            .select_related("organization", "category", "severity", "location__location_type", "assigned_to", "detected_by",
                            "execution", "version__template", "item"))


def get_finding(user: Any, pk: Any) -> Finding | None:
    """One visible finding, or ``None`` (the view answers 404 - existence is not revealed)."""
    parsed = parse_uuid(pk)
    return findings_visible_to(user).filter(pk=parsed).first() if parsed else None


def overdue(queryset: QuerySet[Finding], now: Any = None) -> QuerySet[Finding]:
    """Work still needed past its due date (derived from ``due_at`` and the clock - nothing is persisted)."""
    return queryset.filter(status__in=ACTIVE_STATUSES, due_at__lt=now or timezone.now())


def summary(queryset: QuerySet[Finding], now: Any = None) -> dict[str, Any]:
    """The module dashboard: a few aggregate queries over the *already authorized* queryset, whatever the volume."""
    now = now or timezone.now()
    queryset = queryset.order_by()  # an explicit ordering would be added to the GROUP BY and split the counts
    persisted = dict(queryset.values_list("status").annotate(n=Count("pk")))
    by_status = {s: persisted.get(s, 0) for s, _label in FindingStatus.choices}
    active = queryset.filter(status__in=ACTIVE_STATUSES)
    by_severity = [{"code": code, "name": name, "rank": rank, "count": n}
                   for code, name, rank, n in active.values_list("severity__code", "severity__name", "severity__rank").annotate(n=Count("pk")).order_by("-severity__rank")]
    return {
        "total": sum(persisted.values()),
        "open": sum(by_status[s] for s in ACTIVE_STATUSES),
        "awaiting_closure": sum(by_status[s] for s in AWAITING_CLOSURE),
        "critical": active.filter(severity__is_escalation=True).count(),
        "overdue": overdue(queryset, now).count(),
        "needs_review": active.filter(needs_review=True).count(),
        "by_status": by_status,
        "by_severity": by_severity,
    }


def timeline(finding: Finding, limit: int = 100):  # noqa: ANN201
    """The activity entries of one finding, newest first (the same log every other module writes)."""
    from apps.core.models import ActivityLog

    return (ActivityLog.objects.filter(organization_id=finding.organization_id, metadata__finding_id=str(finding.pk))
            .select_related("actor").order_by("-created_at")[:limit])


def evidence_for(finding: Finding) -> list[Any]:
    """The evidence attachments shown with a finding: the triggering response's *live* attachments (read-through, never copied).

    The caller has already established that the user may see the finding.  Quarantined and unscanned files are returned so the page can
    say so, but only an available asset is ever served or counted (``evidence.selectors.is_servable``).
    """
    if not finding.inherit_evidence or finding.execution_id is None or finding.item_id is None:
        return []
    from apps.evidence.selectors import attachments_for_execution

    return list(attachments_for_execution(finding.execution).filter(item_id=finding.item_id))


def get_evidence_attachment(finding: Finding, pk: Any) -> Any:
    """One attachment of this finding's evidence, or ``None`` - an attachment of any *other* record is never reachable through a finding."""
    parsed = parse_uuid(pk)
    if parsed is None:
        return None
    return next((a for a in evidence_for(finding) if a.pk == parsed), None)


# -- choices (all authorized) ------------------------------------------------------------------------------------------------
def categories_for(organization: Any, *, active_only: bool = True) -> QuerySet[FindingCategory]:
    qs = FindingCategory.objects.filter(organization=organization)
    return qs.filter(is_active=True) if active_only else qs


def severities_for(organization: Any, *, active_only: bool = True) -> QuerySet[FindingSeverity]:
    qs = FindingSeverity.objects.filter(organization=organization)
    return qs.filter(is_active=True) if active_only else qs


def visible_categories(user: Any) -> QuerySet[FindingCategory]:
    return FindingCategory.objects.filter(organization_id__in=visible_organization_ids(user)) if is_active_user(user) else FindingCategory.objects.none()


def visible_severities(user: Any) -> QuerySet[FindingSeverity]:
    return FindingSeverity.objects.filter(organization_id__in=visible_organization_ids(user)) if is_active_user(user) else FindingSeverity.objects.none()


def creatable_locations(user: Any):  # noqa: ANN201
    """Active locations where the user may raise a finding - never another organization's."""
    return get_accessible_locations(user, P.FINDING_CREATE).filter(is_active=True).select_related("location_type", "parent")


def eligible_assignees(organization: Any, location: Any) -> QuerySet:
    """Users who may be made responsible at ``location``: active, active members, and able to work findings there."""
    users = get_user_model().objects.filter(
        is_active=True, organization_memberships__organization=organization, organization_memberships__is_active=True,
    ).distinct().order_by("email")
    ids = [u.pk for u in users if has_permission(u, P.FINDING_RESOLVE, organization=organization, location=location)]
    return get_user_model().objects.filter(pk__in=ids).order_by("email")
