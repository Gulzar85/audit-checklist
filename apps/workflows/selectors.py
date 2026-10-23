"""Read-only queries for workflows.  Authorization happens *first*; every list starts from one of the ``*_visible_to`` functions.

* Definitions: organizations where the user holds ``workflow.view`` (organization-wide) plus the system workflows (read-only).
* Instances: locations where the user holds ``workflow.instance_view`` (subtrees expanded), plus workflows they started or hold a task in.
* Tasks: assigned to the user, or at locations where they hold ``workflow.task_view``; the *claimable* tasks the user could take are computed from the live
  assignee policy (bounded).  Seeing a task never grants access to the business record - that is checked through the record's own policy.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Count, Q, QuerySet
from django.utils import timezone

from apps.core.policies import is_active_user
from apps.core.utils import parse_uuid
from apps.permissions.authorization import get_access, has_permission, visible_organization_ids
from apps.permissions.registry import P

from . import assignment
from .constants import OPEN_TASK_STATUSES, InstanceStatus, TaskStatus
from .models import WorkflowDefinition, WorkflowEvent, WorkflowInstance, WorkflowTask, WorkflowVersion
from .targets import get_spec


def _scoped_orgs_with(user: Any, code: str) -> list[Any]:
    return [oid for oid in visible_organization_ids(user) if has_permission(user, code, organization=oid)] if is_active_user(user) else []


def definitions_visible_to(user: Any) -> QuerySet[WorkflowDefinition]:
    orgs = _scoped_orgs_with(user, P.WORKFLOW_VIEW)
    if not orgs:
        return WorkflowDefinition.objects.none()
    return WorkflowDefinition.objects.filter(Q(organization_id__in=orgs) | Q(organization__isnull=True)).select_related("organization")


def get_definition(user: Any, pk: Any) -> WorkflowDefinition | None:
    parsed = parse_uuid(pk)
    return definitions_visible_to(user).filter(pk=parsed).first() if parsed else None


def versions_visible_to(user: Any) -> QuerySet[WorkflowVersion]:
    return WorkflowVersion.objects.filter(definition__in=definitions_visible_to(user).values("pk")).select_related("definition", "definition__organization")


def get_version(user: Any, pk: Any) -> WorkflowVersion | None:
    parsed = parse_uuid(pk)
    return versions_visible_to(user).filter(pk=parsed).first() if parsed else None


def instances_visible_to(user: Any) -> QuerySet[WorkflowInstance]:
    if not is_active_user(user):
        return WorkflowInstance.objects.none()
    condition = Q()
    org_ids = visible_organization_ids(user)
    for organization_id in org_ids:
        access = get_access(user, organization_id)
        if access.is_org_wide(P.WORKFLOW_INSTANCE_VIEW):
            condition |= Q(organization_id=organization_id)
        elif access.holds(P.WORKFLOW_INSTANCE_VIEW):
            condition |= Q(location_id__in=access.location_ids(P.WORKFLOW_INSTANCE_VIEW))
    if org_ids:
        condition |= Q(organization_id__in=org_ids) & (Q(started_by=user) | Q(tasks__assignee=user))
    if not condition:
        return WorkflowInstance.objects.none()
    return WorkflowInstance.objects.filter(condition).distinct().select_related("organization", "definition", "version", "state", "location", "started_by")


def get_instance(user: Any, pk: Any) -> WorkflowInstance | None:
    parsed = parse_uuid(pk)
    return instances_visible_to(user).filter(pk=parsed).first() if parsed else None


def tasks_visible_to(user: Any) -> QuerySet[WorkflowTask]:
    if not is_active_user(user):
        return WorkflowTask.objects.none()
    condition = Q(assignee=user)
    org_ids = visible_organization_ids(user)
    for organization_id in org_ids:
        access = get_access(user, organization_id)
        if access.is_org_wide(P.WORKFLOW_TASK_VIEW):
            condition |= Q(instance__organization_id=organization_id)
        elif access.holds(P.WORKFLOW_TASK_VIEW):
            condition |= Q(instance__location_id__in=access.location_ids(P.WORKFLOW_TASK_VIEW))
    return WorkflowTask.objects.filter(condition).select_related("instance__definition", "instance__version", "instance__location", "state", "assignee")


def get_task(user: Any, pk: Any) -> WorkflowTask | None:
    parsed = parse_uuid(pk)
    return tasks_visible_to(user).filter(pk=parsed).first() if parsed else None


def claimable_for(user: Any, limit: int = 200) -> list[WorkflowTask]:
    """Open, unassigned group tasks the user is eligible to take (live policy evaluation, bounded)."""
    if not is_active_user(user):
        return []
    out = []
    candidates = (WorkflowTask.objects.filter(status=TaskStatus.PENDING, assignee__isnull=True, instance__status=InstanceStatus.ACTIVE, instance__organization_id__in=visible_organization_ids(user))
                  .select_related("instance__definition", "instance__organization", "instance__location", "state").order_by("due_at", "created_at")[: limit * 3])
    for task in candidates:
        policy = task.state.assignee_policy
        if task.entry_seq != task.instance.entry_seq or not policy or not assignment.eligible(user, policy, task.instance.organization, task.instance.location):
            continue
        spec = get_spec(task.instance.target_type)
        obj = spec.resolve_system(task.instance.target_id)
        if obj is None or not spec.can_view(user, obj) or assignment.violations(user, task.instance, spec, obj, task.state.separation):
            continue
        out.append(task)
        if len(out) >= limit:
            break
    return out


def my_tasks(user: Any) -> QuerySet[WorkflowTask]:
    """Open tasks assigned to the user."""
    return tasks_visible_to(user).filter(assignee=user, status__in=OPEN_TASK_STATUSES, instance__status=InstanceStatus.ACTIVE)


def overdue_tasks(queryset: QuerySet[WorkflowTask], now: Any = None) -> QuerySet[WorkflowTask]:
    return queryset.filter(status__in=OPEN_TASK_STATUSES, due_at__lt=now or timezone.now(), instance__status=InstanceStatus.ACTIVE)


def task_summary(user: Any) -> dict[str, Any]:
    visible = tasks_visible_to(user).order_by()
    mine = my_tasks(user).order_by()
    counts = dict(visible.values_list("status").annotate(n=Count("pk")))
    return {"mine": mine.count(), "mine_overdue": overdue_tasks(mine).count(), "claimable": len(claimable_for(user)), "visible_open": sum(counts.get(s, 0) for s in OPEN_TASK_STATUSES),
            "visible_completed": counts.get(TaskStatus.COMPLETED, 0), "needs_attention": instances_visible_to(user).filter(status=InstanceStatus.ACTIVE, needs_attention=True).count()}


def history(instance: WorkflowInstance) -> QuerySet[WorkflowEvent]:
    return WorkflowEvent.objects.filter(instance=instance).select_related("actor", "from_state", "to_state", "task").order_by("-seq")


def timeline(instance: WorkflowInstance, limit: int = 100):  # noqa: ANN201
    from apps.core.models import ActivityLog

    return (ActivityLog.objects.filter(organization_id=instance.organization_id, metadata__workflow_instance_id=str(instance.pk))
            .select_related("actor").order_by("-created_at")[:limit])


def active_instance_for(target_type: str, target_id: Any) -> WorkflowInstance | None:
    return WorkflowInstance.objects.filter(target_type=target_type, target_id=target_id, status=InstanceStatus.ACTIVE).select_related("definition", "state").first()
