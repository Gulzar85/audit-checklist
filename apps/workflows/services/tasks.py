"""Task operations: claim, release, reassign, and the orphan policy.

A task is never an authorization grant.  Claiming needs the claim permission at the instance's location *and* eligibility under the state's assignee
policy *and* separation of duties *and* visibility of the business record.  Reassigning needs the reassign permission and an eligible new assignee.
Tasks cannot change status any other way: completion happens only through a configured transition (``engine.perform``).

Orphan policy (an assignee became inactive or lost eligibility): ``release_orphaned_tasks`` returns the task to the claimable pool (group slot) or,
for a task named for one person, clears the assignee and flags the workflow for administrative attention - a task is never silently discarded.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.core.services import stamp_history

from .. import assignment, policies
from ..constants import OPEN_TASK_STATUSES, EventKind, TaskStatus
from ..models import WorkflowInstance, WorkflowTask
from ..targets import get_spec
from .base import VERBS, lock_instance, log, record_event
from .errors import WorkflowConflict, WorkflowError


def _lock(task: WorkflowTask) -> tuple[WorkflowInstance, WorkflowTask]:
    instance = lock_instance(task.instance_id)
    fresh = WorkflowTask.objects.select_for_update(of=("self",)).select_related("state").get(pk=task.pk)
    if not instance.is_active or not fresh.is_open or fresh.entry_seq != instance.entry_seq:
        raise WorkflowConflict(_("This task is no longer open."), "task_closed")
    return instance, fresh


def _check_candidate(user: Any, instance: WorkflowInstance, task: WorkflowTask) -> None:
    spec = get_spec(instance.target_type)
    obj = spec.resolve_system(instance.target_id)
    policy = task.state.assignee_policy
    if obj is None or not policy:
        raise WorkflowError(_("The task cannot be assigned."), "task_closed")
    if not assignment.eligible(user, policy, instance.organization, instance.location) or not spec.can_view(user, obj):
        raise WorkflowError(_("That user is not eligible for this task."), "invalid_assignee")
    if assignment.violations(user, instance, spec, obj, task.state.separation):
        raise WorkflowError(_("That user cannot take this task (separation of duties)."), "separation_of_duties")


@transaction.atomic
def claim(task: WorkflowTask, *, actor: Any, expected_revision: int | None = None) -> WorkflowTask:
    instance, fresh = _lock(task)
    if not policies.can_claim_at(actor, instance.location):
        raise PermissionDenied
    if fresh.assignee_id == actor.pk and fresh.status == TaskStatus.CLAIMED:
        return fresh  # idempotent: already yours
    if expected_revision is not None and expected_revision != fresh.revision:
        raise WorkflowConflict(_("The task changed since you loaded it."), "stale_revision")
    if fresh.assignee_id is not None:
        raise WorkflowConflict(_("This task is already taken."), "already_claimed")
    try:
        _check_candidate(actor, instance, fresh)
    except WorkflowError as exc:
        raise PermissionDenied from exc
    fresh.assignee, fresh.status, fresh.revision = actor, TaskStatus.CLAIMED, fresh.revision + 1
    stamp_history(fresh, actor, VERBS["task_claimed"])
    fresh.save()
    record_event(instance, EventKind.TASK_CLAIMED, actor, task=fresh)
    log(VERBS["task_claimed"], instance, actor, f"Task claimed for {fresh.state.name}", task_id=str(fresh.pk))
    return fresh


@transaction.atomic
def release(task: WorkflowTask, *, actor: Any) -> WorkflowTask:
    """Give a claimed group task back to the pool (the claimant, or someone who may reassign tasks)."""
    instance, fresh = _lock(task)
    if fresh.assignee_id is None:
        return fresh
    if fresh.assignee_id != actor.pk and not policies.can_reassign_at(actor, instance.location):
        raise PermissionDenied
    if fresh.slot != "group":
        raise WorkflowConflict(_("A task named for one person cannot be released; ask for it to be reassigned."), "not_releasable")
    fresh.assignee, fresh.status, fresh.revision = None, TaskStatus.PENDING, fresh.revision + 1
    stamp_history(fresh, actor, VERBS["task_released"])
    fresh.save()
    record_event(instance, EventKind.TASK_RELEASED, actor, task=fresh)
    log(VERBS["task_released"], instance, actor, f"Task released for {fresh.state.name}", task_id=str(fresh.pk))
    return fresh


@transaction.atomic
def reassign(task: WorkflowTask, *, actor: Any, assignee: Any) -> WorkflowTask:
    instance, fresh = _lock(task)
    if not policies.can_reassign_at(actor, instance.location):
        raise PermissionDenied
    if assignee is None:
        raise WorkflowError(_("Choose who the task is reassigned to."), "invalid_assignee")
    _check_candidate(assignee, instance, fresh)
    previous = fresh.assignee_id
    if previous == assignee.pk:
        return fresh
    fresh.assignee, fresh.status, fresh.revision = assignee, TaskStatus.CLAIMED, fresh.revision + 1
    stamp_history(fresh, actor, VERBS["task_reassigned"])
    fresh.save()
    if instance.needs_attention:
        instance.needs_attention = False
        instance.save(update_fields=["needs_attention", "updated_at"])
    record_event(instance, EventKind.TASK_REASSIGNED, actor, task=fresh, metadata={"from": str(previous) if previous else None, "to": str(assignee.pk)})
    log(VERBS["task_reassigned"], instance, actor, f"Task reassigned to {assignee.get_short_name()}", task_id=str(fresh.pk), changes={"from": str(previous) if previous else None, "to": str(assignee.pk)})
    return fresh


def release_orphaned_tasks(*, organization: Any = None, actor: Any = None) -> int:
    """Housekeeping (``manage.py release_workflow_orphans``): open tasks whose assignee is inactive or no longer eligible are released / flagged."""
    released = 0
    qs = WorkflowTask.objects.filter(status__in=OPEN_TASK_STATUSES, assignee__isnull=False).select_related("instance", "state", "assignee")
    if organization is not None:
        qs = qs.filter(instance__organization=organization)
    for task in qs:
        with transaction.atomic():
            try:
                instance, fresh = _lock(task)
            except WorkflowConflict:
                continue
            policy = fresh.state.assignee_policy
            if fresh.assignee is None or (policy and assignment.eligible(fresh.assignee, policy, instance.organization, instance.location)):
                continue
            previous = fresh.assignee_id
            fresh.assignee, fresh.status, fresh.revision = None, TaskStatus.PENDING, fresh.revision + 1
            fresh.save()
            if fresh.slot != "group":
                instance.needs_attention = True
                instance.save(update_fields=["needs_attention", "updated_at"])
            record_event(instance, EventKind.TASK_RELEASED, actor, task=fresh, metadata={"orphaned_from": str(previous), "needs_attention": fresh.slot != "group"})
            log(VERBS["task_released"], instance, actor, "Task released: the assignee is no longer eligible", task_id=str(fresh.pk), orphaned_from=str(previous))
            released += 1
    return released
