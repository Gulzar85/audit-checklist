"""Read-only query helpers for executions; every result is authorized before it is returned."""

from __future__ import annotations

from typing import Any

from django.db.models import Q, QuerySet

from apps.assignments.models import ChecklistAssignment
from apps.core.policies import is_active_user
from apps.permissions.authorization import get_accessible_locations, has_permission_anywhere
from apps.permissions.registry import P

from . import policies
from .constants import ExecutionStatus
from .models import ChecklistExecution


def my_assignments(user: Any) -> QuerySet[ChecklistAssignment]:
    """Open assignments the user may execute: executable status, a location where they hold ``execution.create``, and the
    designation rule (their own, or undesignated)."""
    if not is_active_user(user) or not has_permission_anywhere(user, P.EXECUTION_CREATE):
        return ChecklistAssignment.objects.none()
    locations = get_accessible_locations(user, P.EXECUTION_CREATE)
    return (
        ChecklistAssignment.objects.filter(status__in=policies.EXECUTABLE_ASSIGNMENT_STATUSES, location__in=locations.values("pk"))
        .filter(Q(assigned_to=user) | Q(assigned_to__isnull=True))
        .select_related("organization", "version__template", "location", "assigned_to")
    )


def executions_visible_to(user: Any) -> QuerySet[ChecklistExecution]:
    """Executions at locations where the user holds ``execution.view``, plus ones they started or are designated for."""
    if not is_active_user(user):
        return ChecklistExecution.objects.none()
    scoped = Q(pk__in=[])
    if has_permission_anywhere(user, P.EXECUTION_VIEW):
        scoped = Q(assignment__location__in=get_accessible_locations(user, P.EXECUTION_VIEW).values("pk"))
    own = Q(started_by=user) | Q(assignment__assigned_to=user)
    return (ChecklistExecution.objects.filter(scoped | own).distinct()
            .select_related("organization", "assignment__location", "assignment__version__template", "version__template", "started_by", "submitted_by"))


def get_execution(user: Any, pk: Any) -> ChecklistExecution | None:
    """The execution if the user may see it, else ``None`` (the caller answers 404)."""
    from apps.core.utils import parse_uuid

    parsed = parse_uuid(pk)
    if parsed is None:
        return None
    return executions_visible_to(user).filter(pk=parsed).first()


def execution_for_assignment(user: Any, assignment: ChecklistAssignment) -> ChecklistExecution | None:
    """The assignment's live execution (in progress or submitted) if the user may see it."""
    return executions_visible_to(user).filter(assignment=assignment, status__in=[ExecutionStatus.IN_PROGRESS, ExecutionStatus.SUBMITTED]).first()


def timeline(execution: ChecklistExecution, limit: int = 50):  # noqa: ANN201
    """Audit entries of one execution, newest first."""
    from apps.core.models import ActivityLog

    return (ActivityLog.objects.filter(organization_id=execution.organization_id, metadata__execution_id=str(execution.pk))
            .select_related("actor").order_by("-created_at")[:limit])


def assignment_for_work(user: Any, pk: Any) -> ChecklistAssignment | None:
    """An assignment the user may start/resume (open to them), or whose live execution they may see; ``None`` -> 404."""
    from apps.core.utils import parse_uuid

    parsed = parse_uuid(pk)
    if parsed is None:
        return None
    found = my_assignments(user).filter(pk=parsed).first()
    if found is not None:
        return found
    live = (executions_visible_to(user).filter(assignment_id=parsed, status__in=[ExecutionStatus.IN_PROGRESS, ExecutionStatus.SUBMITTED])
            .select_related("assignment__location", "assignment__organization", "assignment__version__template").first())
    return live.assignment if live else None
