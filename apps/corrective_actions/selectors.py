"""Read-only queries for corrective actions.  Authorization happens *first*: every list starts from ``actions_visible_to``.

Visible = in an organization the user may enter, and either at a location where they hold ``corrective_action.view`` (organization-wide
or scoped, subtrees expanded) or responsible for the action.  Filters, search and pagination are applied on top, so a parameter can never
reach another scope.  The verification queue is narrower still: only submitted actions the user could actually decide.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth import get_user_model
from django.db.models import Case, Count, IntegerField, Q, QuerySet, Value, When
from django.utils import timezone

from apps.core.policies import is_active_user
from apps.core.utils import parse_uuid
from apps.permissions.authorization import get_access, get_accessible_locations, has_permission, visible_organization_ids
from apps.permissions.registry import P

from .constants import OUTSTANDING_STATUSES, PRIORITY_RANK, WORKING_STATUSES, ActionStatus
from .models import CorrectiveAction


def _rank():  # noqa: ANN202
    return Case(*(When(priority=p, then=Value(r)) for p, r in PRIORITY_RANK.items()), default=Value(0), output_field=IntegerField())


def actions_visible_to(user: Any, organization: Any = None) -> QuerySet[CorrectiveAction]:
    """The authorized base queryset (list, detail, filters, API, admin and summaries all start here)."""
    if not is_active_user(user):
        return CorrectiveAction.objects.none()
    org_ids = visible_organization_ids(user)
    if organization is not None:
        wanted = str(getattr(organization, "pk", organization))
        org_ids = [pk for pk in org_ids if str(pk) == wanted]
    condition = Q()
    for organization_id in org_ids:
        access = get_access(user, organization_id)
        if access.is_org_wide(P.CORRECTIVE_ACTION_VIEW):
            condition |= Q(organization_id=organization_id)
        elif access.holds(P.CORRECTIVE_ACTION_VIEW):
            condition |= Q(location_id__in=access.location_ids(P.CORRECTIVE_ACTION_VIEW))
    if org_ids:
        condition |= Q(assigned_to=user, organization_id__in=org_ids)
    if not condition:
        return CorrectiveAction.objects.none()
    return (CorrectiveAction.objects.filter(condition).annotate(priority_order=_rank())
            .select_related("organization", "finding__severity", "finding__category", "location__location_type", "assigned_to", "created_by", "submitted_by", "verified_by"))


def get_action(user: Any, pk: Any) -> CorrectiveAction | None:
    """One visible action, or ``None`` (the view answers 404 - existence is not revealed)."""
    parsed = parse_uuid(pk)
    return actions_visible_to(user).filter(pk=parsed).first() if parsed else None


def overdue(queryset: QuerySet[CorrectiveAction], now: Any = None) -> QuerySet[CorrectiveAction]:
    """The owner still has work and the due date has passed (derived from ``due_at`` and the clock - nothing is persisted)."""
    return queryset.filter(status__in=WORKING_STATUSES, due_at__lt=now or timezone.now())


def for_finding(user: Any, finding: Any) -> QuerySet[CorrectiveAction]:
    return actions_visible_to(user).filter(finding=finding)


def my_actions(user: Any) -> QuerySet[CorrectiveAction]:
    """Actions assigned to the user that are not finished: the worklist (drafts are not theirs to work on yet)."""
    return actions_visible_to(user).filter(assigned_to=user).exclude(status__in=[ActionStatus.DRAFT, ActionStatus.CLOSED, ActionStatus.CANCELLED])


def verification_queue(user: Any) -> QuerySet[CorrectiveAction]:
    """Submitted actions the user may decide: ``verify`` at the location *and* separation of duties satisfied."""
    if not is_active_user(user):
        return CorrectiveAction.objects.none()
    condition = Q()
    for organization_id in visible_organization_ids(user):
        access = get_access(user, organization_id)
        if access.is_org_wide(P.CORRECTIVE_ACTION_VERIFY):
            condition |= Q(organization_id=organization_id)
        elif access.holds(P.CORRECTIVE_ACTION_VERIFY):
            condition |= Q(location_id__in=access.location_ids(P.CORRECTIVE_ACTION_VERIFY))
    if not condition:
        return CorrectiveAction.objects.none()
    qs = actions_visible_to(user).filter(condition, status=ActionStatus.SUBMITTED)
    # Independence: the owner and the submitter never see their own work in the queue (the policy re-checks on the decision itself).
    qs = qs.exclude(Q(independent_verification=True) & (Q(assigned_to=user) | Q(submitted_by=user)))
    from django.conf import settings

    if getattr(settings, "CORRECTIVE_ACTION_VERIFIER_NOT_CREATOR", False):
        qs = qs.exclude(Q(independent_verification=True) & Q(created_by=user))
    return qs.order_by("submitted_at")


def summary(queryset: QuerySet[CorrectiveAction], now: Any = None) -> dict[str, Any]:
    """The module dashboard: a few aggregate queries over the *already authorized* queryset, whatever the volume."""
    now = now or timezone.now()
    queryset = queryset.order_by()  # an explicit ordering would be added to the GROUP BY and split the counts
    persisted = dict(queryset.values_list("status").annotate(n=Count("pk")))
    by_status = {s: persisted.get(s, 0) for s, _label in ActionStatus.choices}
    return {
        "total": sum(persisted.values()),
        "open": sum(by_status[s] for s in OUTSTANDING_STATUSES if s != ActionStatus.DRAFT),
        "in_progress": by_status[ActionStatus.IN_PROGRESS],
        "blocked": by_status[ActionStatus.BLOCKED],
        "awaiting_verification": by_status[ActionStatus.SUBMITTED],
        "rejected": by_status[ActionStatus.REJECTED],
        "overdue": overdue(queryset, now).count(),
        "verified": by_status[ActionStatus.VERIFIED],
        "by_status": by_status,
    }


def timeline(action: CorrectiveAction, limit: int = 100):  # noqa: ANN201
    from apps.core.models import ActivityLog

    return (ActivityLog.objects.filter(organization_id=action.organization_id, metadata__action_id=str(action.pk))
            .select_related("actor").order_by("-created_at")[:limit])


def evidence_for(action: CorrectiveAction, *, include_detached: bool = False) -> list[Any]:
    from apps.evidence.models import EvidenceAttachment

    qs = EvidenceAttachment.objects.filter(corrective_action=action).select_related("asset", "attached_by").order_by("position", "created_at")
    return list(qs if include_detached else qs.filter(detached_at__isnull=True))


def verified_completion_count(action: CorrectiveAction) -> int:
    """Live completion files that count towards the requirement: only *available* (scanned) assets - never pending or quarantined ones."""
    from apps.evidence.constants import AssetStatus
    from apps.evidence.models import EvidenceAttachment

    from .constants import Purpose

    return EvidenceAttachment.objects.filter(corrective_action=action, purpose=Purpose.COMPLETION, detached_at__isnull=True,
                                             asset__status=AssetStatus.AVAILABLE).count()


def eligible_assignees(organization: Any, location: Any) -> QuerySet:
    """Users who may be made responsible at ``location``: active, active members, able to carry out actions there."""
    users = get_user_model().objects.filter(
        is_active=True, organization_memberships__organization=organization, organization_memberships__is_active=True,
    ).distinct().order_by("email")
    ids = [u.pk for u in users if has_permission(u, P.CORRECTIVE_ACTION_START, organization=organization, location=location)]
    return get_user_model().objects.filter(pk__in=ids).order_by("email")


def creatable_findings(user: Any) -> QuerySet:
    """Findings the user may add actions to (for pickers): visible findings at locations where they hold ``corrective_action.create``."""
    from apps.findings import selectors as finding_selectors
    from apps.findings.constants import EDITABLE_STATUSES

    locations = get_accessible_locations(user, P.CORRECTIVE_ACTION_CREATE)
    return finding_selectors.findings_visible_to(user).filter(status__in=EDITABLE_STATUSES, location__in=locations.values("pk"))
