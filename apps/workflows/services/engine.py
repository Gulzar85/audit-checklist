"""The workflow engine: starting a workflow on a registered business record, executing transitions, cancelling.

``perform`` is the **only** way an instance changes state.  It re-reads everything from the server (never a client-supplied state, target or actor),
and in one transaction under the instance row lock: replays a repeated request (durable idempotency key), checks revision / activity, selects the
configured branch with the Phase 6 conditions (an unknown condition fails closed), authorizes the actor (task assignee / starter / authorized, plus
separation of duties), records approvals (multi-approver modes), invokes the registered business action through the domain's own lifecycle service,
moves the instance, completes or creates tasks, and appends the event.  If the domain service refuses, the transaction rolls back - the workflow never
claims a business operation that did not happen; the refusal is audited outside the rolled-back transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.checklists.rules.tri import Tri
from apps.core.services import stamp_history
from apps.permissions.registry import P

from .. import assignment, conditions, events, handlers, policies
from ..constants import (
    OPEN_TASK_STATUSES, ActorRule, ApprovalMode, Decision, DefinitionStatus, EventKind, InstanceStatus, OnNoAssignee, Outcome, StateKind, TaskStatus,
    VersionStatus,
)
from ..models import WorkflowDefinition, WorkflowEvent, WorkflowInstance, WorkflowState, WorkflowTask, WorkflowTransition
from ..targets import TargetNotFound, get_spec
from .base import VERBS, integrity_error, lock_instance, lock_organization, log, next_number, record_event
from .errors import WorkflowConflict, WorkflowError


@dataclass
class TransitionResult:
    instance: WorkflowInstance
    event: WorkflowEvent | None = None
    replayed: bool = False
    #: Set when an approval was recorded but more are required before the transition fires.
    pending_approvals: int | None = None
    outcome: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------------------------------------------------------------
def resolve_definition(organization: Any, target_type: str, code: str | None = None) -> WorkflowDefinition | None:
    """The workflow to start for a record: by code, else the organization's default for the target type, else the system default (override precedence)."""
    qs = WorkflowDefinition.objects.filter(status=DefinitionStatus.ACTIVE, target_type=target_type, versions__status=VersionStatus.PUBLISHED).distinct()
    if code:
        return qs.filter(code=code, organization=organization).first() or qs.filter(code=code, organization__isnull=True).first()
    return qs.filter(is_default=True, organization=organization).first() or qs.filter(is_default=True, organization__isnull=True).first()


def _published(definition: WorkflowDefinition) -> Any:
    return definition.versions.filter(status=VersionStatus.PUBLISHED).first()


# ---------------------------------------------------------------------------------------------------------------------------------
def start(*, actor: Any, target_type: str, target: Any, definition: WorkflowDefinition | None = None, definition_code: str | None = None,
          system: bool = False) -> WorkflowInstance:
    """Start a workflow on a business record.  ``target`` is an id (resolved through the authorized selector) or, for ``system`` starts
    (domain events), a record the caller already holds.  Transactional: the instance, its initial tasks and its start event exist together or not at all."""
    try:
        spec = get_spec(target_type)
    except TargetNotFound:
        raise WorkflowError(_("Unknown target type."), "invalid_target_type") from None
    obj = target if system and not isinstance(target, (str, bytes)) and hasattr(target, "pk") else (
        spec.resolve_system(target) if system else spec.resolve(actor, target))
    if obj is None:
        raise WorkflowError(_("That record was not found."), "target_not_found")
    organization, location = spec.organization(obj), spec.location(obj)
    if not system and not policies.can_start(actor, location):
        raise PermissionDenied
    problem = spec.eligible(obj)
    if problem:
        raise WorkflowError(problem, "target_not_eligible")
    definition = definition or resolve_definition(organization, spec.type, definition_code)
    if definition is None:
        raise WorkflowError(_("No published workflow is configured for these records."), "no_workflow")
    if definition.target_type != spec.type or (definition.organization_id is not None and definition.organization_id != organization.pk):
        raise WorkflowError(_("That workflow does not apply to this record."), "no_workflow")
    version = _published(definition)
    if definition.status != DefinitionStatus.ACTIVE or version is None:
        raise WorkflowError(_("That workflow has no published version."), "no_workflow")
    if conditions.evaluate(spec.type, version.start_condition, spec.facts(obj)) is not Tri.TRUE:
        raise WorkflowError(_("This record does not meet the workflow's start condition."), "start_condition_not_met")
    return _start(actor, spec, obj, organization, location, definition, version, system)


@transaction.atomic
def _start(actor: Any, spec: Any, obj: Any, organization: Any, location: Any, definition: Any, version: Any, system: bool) -> WorkflowInstance:
    if WorkflowInstance.objects.filter(target_type=spec.type, target_id=obj.pk, definition=definition, status=InstanceStatus.ACTIVE).exists():
        raise WorkflowConflict(_("This record already has an active workflow of this kind."), "already_running")
    initial = version.states.get(kind=StateKind.INITIAL)
    lock_organization(organization.pk)
    now = timezone.now()
    instance = WorkflowInstance(
        organization=organization, number=next_number(organization.pk), definition=definition, version=version, target_type=spec.type, target_id=obj.pk, location=location,
        state=initial, started_by=actor if getattr(actor, "is_authenticated", False) else None, system_started=system, started_at=now, state_entered_at=now,
    )
    stamp_history(instance, actor, VERBS["started"])
    try:
        with transaction.atomic():
            instance.save()
    except IntegrityError as exc:
        raise integrity_error(exc) from exc
    record_event(instance, EventKind.STARTED, actor, to_state=initial, metadata={"version": version.number, "config_hash": version.config_hash, "system": system})
    log(VERBS["started"], instance, actor, f"Started {definition.name} v{version.number} for {spec.reference(obj)}", state=initial.code, system=system, config_hash=version.config_hash)
    _enter_state(instance, initial, spec, obj, actor)
    return instance


# ---------------------------------------------------------------------------------------------------------------------------------
def _enter_state(instance: WorkflowInstance, state: WorkflowState, spec: Any, obj: Any, actor: Any) -> None:
    """Create the tasks of a state just entered (idempotent per slot).  An unresolvable policy blocks (rolls back) or flags the workflow, as configured."""
    instance.state_entered_at = timezone.now()
    log(VERBS["state_entered"], instance, actor, f"Entered {state.name}", state=state.code, entry_seq=instance.entry_seq)
    policy = state.assignee_policy
    if not policy or state.is_terminal:
        return
    candidates = [u for u in assignment.candidates(policy, instance.organization, instance.location)
                  if spec.can_view(u, obj) and not assignment.violations(u, instance, spec, obj, state.separation)]
    due = instance.state_entered_at + timedelta(hours=state.due_hours) if state.due_hours else None
    if not candidates:
        if state.on_no_assignee == OnNoAssignee.BLOCK:
            raise WorkflowError(_("Nobody is eligible to handle “%(state)s”; the workflow cannot proceed.") % {"state": state.name}, "no_assignee")
        instance.needs_attention = True
        record_event(instance, EventKind.ATTENTION, actor, to_state=state, metadata={"reason": "no eligible assignee"})
        log(VERBS["attention"], instance, actor, f"No eligible assignee for {state.name}", state=state.code)
        WorkflowTask.objects.get_or_create(instance=instance, entry_seq=instance.entry_seq, slot="group", defaults={"state": state, "due_at": due})
        return
    slots = [(str(u.pk), u) for u in candidates] if policy["type"] == "user" else [("group", None)]
    for slot, user in slots:
        task, created = WorkflowTask.objects.get_or_create(instance=instance, entry_seq=instance.entry_seq, slot=slot,
                                                           defaults={"state": state, "assignee": user, "due_at": due})
        if created:
            record_event(instance, EventKind.TASK_CREATED, actor, task=task, to_state=state, metadata={"slot": slot, "assignee": str(user.pk) if user else None})
            log(VERBS["task_created"], instance, actor, f"Task created for {state.name}", task_id=str(task.pk), slot=slot)
            events.send_on_commit(events.task_created, instance, actor, task=task)


# ---------------------------------------------------------------------------------------------------------------------------------
class _NoBranch:  # a sentinel: transitions exist for the action but no condition holds
    pass


_NO_BRANCH = _NoBranch()


def _branch(instance: WorkflowInstance, action: str, facts: dict[str, Any]) -> WorkflowTransition | None:
    """The configured branch for ``action`` from the current state: the first whose condition is TRUE (unknown never selects one)."""
    options = list(instance.version.transitions.filter(source=instance.state, action=action, enabled=True).select_related("source", "target").order_by("sort_order"))
    if not options:
        return None
    for t in options:
        if conditions.evaluate(instance.target_type, t.condition, facts) is Tri.TRUE:
            return t
    return _NO_BRANCH


def _open_tasks(instance: WorkflowInstance) -> list[WorkflowTask]:
    return list(WorkflowTask.objects.filter(instance=instance, entry_seq=instance.entry_seq, status__in=OPEN_TASK_STATUSES).select_related("state"))


def _actor_task(instance: WorkflowInstance, actor: Any, tasks: list[WorkflowTask]) -> WorkflowTask | None:
    """The task the actor may act through: their own, or the group task they are eligible to claim (the claim happens when they act)."""
    for t in tasks:
        if t.assignee_id == actor.pk:
            return t
    policy = instance.state.assignee_policy
    if policy:
        for t in tasks:
            if t.slot == "group" and t.assignee_id is None and assignment.eligible(actor, policy, instance.organization, instance.location):
                return t
    return None


def _authorize(instance: WorkflowInstance, transition: WorkflowTransition, actor: Any, spec: Any, obj: Any) -> WorkflowTask | None:
    """Raise ``PermissionDenied`` unless the actor may perform the transition; return the task acted through (task-based transitions)."""
    if transition.actor_rule == ActorRule.ASSIGNEE:
        task = _actor_task(instance, actor, _open_tasks(instance))
        if task is None:
            if WorkflowEvent.objects.filter(instance=instance, kind=EventKind.APPROVAL, entry_seq=instance.entry_seq, actor=actor).exists():
                raise WorkflowConflict(_("You have already recorded your approval for this step."), "already_approved")
            raise PermissionDenied
        if not spec.can_view(actor, obj):
            raise PermissionDenied
        broken = assignment.violations(actor, instance, spec, obj, instance.state.separation)
        if broken:
            raise PermissionDenied
        return task
    if transition.actor_rule == ActorRule.STARTER:
        if not (instance.started_by_id == actor.pk or policies.can_transition_authorized(actor, instance)):
            raise PermissionDenied
    elif not policies.can_transition_authorized(actor, instance):
        raise PermissionDenied
    if assignment.violations(actor, instance, spec, obj, instance.state.separation):
        raise PermissionDenied
    return None


def available_transitions(instance: WorkflowInstance, actor: Any) -> list[dict[str, Any]]:
    """What the actor could do *now* (read-only; the server re-validates every submission).  One entry per action, with the branch that would be taken."""
    if not instance.is_active:
        return []
    spec = get_spec(instance.target_type)
    obj = spec.resolve_system(instance.target_id)
    if obj is None:
        return []
    facts = spec.facts(obj)
    out = []
    for action in dict.fromkeys(instance.version.transitions.filter(source=instance.state, enabled=True).order_by("sort_order", "action", "pk").values_list("action", flat=True)):
        branch = _branch(instance, action, facts)
        if branch is None or branch is _NO_BRANCH:
            continue
        try:
            _authorize(instance, branch, actor, spec, obj)
        except PermissionDenied:
            continue
        out.append({"action": branch.action, "label": branch.label, "decision": branch.decision, "reason_required": branch.reason_required,
                    "target_state": branch.target.code, "target_name": branch.target.name, "handler": branch.handler})
    return out


# ---------------------------------------------------------------------------------------------------------------------------------
def perform(instance: WorkflowInstance, action: str, *, actor: Any, comment: str = "", idempotency_key: Any = None, expected_revision: int | None = None) -> TransitionResult:
    try:
        return _perform(instance, action, actor, (comment or "").strip(), idempotency_key, expected_revision)
    except (WorkflowError, PermissionDenied, ValidationError) as exc:
        fresh = WorkflowInstance.objects.select_related("organization", "definition", "version", "state").get(pk=instance.pk)
        code = getattr(exc, "workflow_code", None) or ("forbidden" if isinstance(exc, PermissionDenied) else getattr(exc, "code", "refused"))
        verb = VERBS["handler_failed"] if code == "handler_failed" else VERBS["transition_refused"]
        log(verb, fresh, actor, f"{action} on {fresh.reference} refused", action=action, code=str(code), state=fresh.state.code)
        raise


@transaction.atomic
def _perform(instance: WorkflowInstance, action: str, actor: Any, comment: str, key: Any, expected_revision: int | None) -> TransitionResult:
    locked = lock_instance(instance.pk)
    if key is not None:
        prior = WorkflowEvent.objects.filter(instance=locked, idempotency_key=key).first()
        if prior is not None:  # a retried request: the same answer, no second execution (durable, not process-local)
            return TransitionResult(locked, prior, replayed=True, outcome=prior.metadata.get("outcome", {}))
    if not locked.is_active:
        raise WorkflowConflict(_("This workflow is %(status)s; nothing can be done.") % {"status": locked.get_status_display().lower()}, "not_active")
    if expected_revision is not None and expected_revision != locked.revision:
        raise WorkflowConflict(_("The workflow changed since you loaded it (revision %(now)d). Reload and try again.") % {"now": locked.revision}, "stale_revision")
    spec = get_spec(locked.target_type)
    obj = spec.resolve_system(locked.target_id)
    if obj is None:
        raise WorkflowError(_("The business record is no longer available."), "target_not_found")
    branch = _branch(locked, action, spec.facts(obj))
    if branch is None:
        raise WorkflowError(_("“%(action)s” is not available in this state.") % {"action": action}, "invalid_transition")
    if branch is _NO_BRANCH:
        raise WorkflowError(_("None of the configured branches for “%(action)s” applies to this record.") % {"action": action}, "no_branch")
    task = _authorize(locked, branch, actor, spec, obj)
    if branch.reason_required and not comment:
        raise WorkflowError(_("A reason is required."), "reason_required")
    if len(comment) > 1000:
        raise WorkflowError(_("The comment is too long."), "too_long")
    state = locked.state
    if task is not None and task.assignee_id is None:  # acting through a group task claims it
        task.assignee, task.status = actor, TaskStatus.CLAIMED
    if branch.decision == Decision.APPROVE and state.approval_mode != ApprovalMode.ONE:
        pending = _record_approval(locked, state, branch, actor, task, comment, key)
        if pending is not None:
            return pending
    outcome: dict[str, Any] = {}
    if branch.handler:
        handler = handlers.get(branch.handler)
        if handler is None or handler.target_type != locked.target_type:
            raise WorkflowError(_("The business action is not available."), "invalid_handler")
        try:
            outcome = handlers.run(handler, actor, obj, comment)
        except (PermissionDenied, ValidationError) as exc:
            raise _HandlerRefused(exc) from exc
    now = timezone.now()
    previous_entry = locked.entry_seq
    for t in _open_tasks(locked):  # the decision closes this visit's tasks: the actor's is completed, the others are no longer needed
        if task is not None and t.pk == task.pk:
            t.assignee, t.status, t.completed_by, t.completed_at = actor, TaskStatus.COMPLETED, actor, now
        else:
            t.status = TaskStatus.CANCELLED
        t.revision += 1
        t.save()
    locked.state, locked.entry_seq, locked.revision, locked.needs_attention = branch.target, previous_entry + 1, locked.revision + 1, False
    if branch.target.is_terminal:
        locked.status = InstanceStatus.COMPLETED
        locked.outcome = Outcome.SUCCESS if branch.target.kind == StateKind.SUCCESS else Outcome.FAILURE
        locked.completed_at = now
    stamp_history(locked, actor, VERBS["transition"])
    try:
        with transaction.atomic():
            locked.save()
    except IntegrityError as exc:
        raise integrity_error(exc) from exc
    try:
        event = record_event(locked, EventKind.TRANSITION, actor, action=action, from_state=state, to_state=branch.target, comment=comment, entry_seq=previous_entry,
                             idempotency_key=key, task=task, metadata={"decision": branch.decision, "handler": branch.handler or None, "outcome": outcome,
                                                                        "branch": branch.sort_order, "revision": locked.revision, "version": locked.version.number})
    except IntegrityError as exc:
        raise integrity_error(exc) from exc
    if branch.handler:
        log(VERBS["handler_invoked"], locked, actor, f"{branch.handler} invoked", handler=branch.handler, outcome=outcome)
    log(VERBS["transition"], locked, actor, f"{branch.label}: {state.name} -> {branch.target.name}", action=action, from_state=state.code, to_state=branch.target.code,
        reason=comment or None, decision=branch.decision)
    if locked.status == InstanceStatus.COMPLETED:
        log(VERBS["completed"], locked, actor, f"Completed ({locked.outcome})", outcome=locked.outcome)
        events.send_on_commit(events.instance_finished, locked, actor)
    else:
        _enter_state(locked, branch.target, spec, obj, actor)
        locked.save(update_fields=["state_entered_at", "needs_attention", "updated_at"])
    return TransitionResult(locked, event, outcome=outcome)


class _HandlerRefused(WorkflowError):
    """The domain service refused the business operation: the whole transition rolls back."""

    def __init__(self, cause: Exception) -> None:
        message = " ".join(getattr(cause, "messages", None) or [str(cause) or "The business action was refused."])
        super().__init__(message, "handler_failed")
        self.__cause__ = cause


def _record_approval(instance: WorkflowInstance, state: WorkflowState, branch: WorkflowTransition, actor: Any, task: WorkflowTask | None, comment: str,
                     key: Any) -> TransitionResult | None:
    """Count this actor's approval for the current visit.  Returns a result while more approvals are needed, ``None`` once the requirement is met."""
    if WorkflowEvent.objects.filter(instance=instance, kind=EventKind.APPROVAL, entry_seq=instance.entry_seq, actor=actor).exists():
        raise WorkflowConflict(_("You have already recorded your approval for this step."), "already_approved")  # a duplicate never counts twice
    try:
        with transaction.atomic():
            event = record_event(instance, EventKind.APPROVAL, actor, action=branch.action, from_state=state, comment=comment, task=task, idempotency_key=key,
                                 metadata={"mode": state.approval_mode})
    except IntegrityError as exc:
        raise integrity_error(exc) from exc
    given = WorkflowEvent.objects.filter(instance=instance, kind=EventKind.APPROVAL, entry_seq=instance.entry_seq).values("actor").distinct().count()
    required = (WorkflowTask.objects.filter(instance=instance, entry_seq=instance.entry_seq).exclude(slot="group").count()
                if state.approval_mode == ApprovalMode.ALL else state.min_approvals)
    log(VERBS["approval"], instance, actor, f"Approval {given} of {required} recorded", given=given, required=required)
    if given >= required:
        return None
    if task is not None and task.slot == "group":  # a shared task stays open so the other eligible approvers can still act
        task.assignee, task.status = None, TaskStatus.PENDING
        task.revision += 1
        task.save()
    elif task is not None:
        task.status, task.completed_by, task.completed_at = TaskStatus.COMPLETED, actor, timezone.now()
        task.revision += 1
        task.save()
    return TransitionResult(instance, event, pending_approvals=required - given)


# ---------------------------------------------------------------------------------------------------------------------------------
@transaction.atomic
def cancel(instance: WorkflowInstance, *, actor: Any, reason: str) -> WorkflowInstance:
    """Cancel a running workflow: its open tasks are cancelled; the business record is **not** touched."""
    locked = lock_instance(instance.pk)
    if not policies._at(actor, P.WORKFLOW_CANCEL, locked.location):
        raise PermissionDenied
    reason = (reason or "").strip()
    if not locked.is_active:
        raise WorkflowConflict(_("This workflow is already %(status)s.") % {"status": locked.get_status_display().lower()}, "not_active")
    if not reason or len(reason) > 300:
        raise WorkflowError(_("A reason of up to 300 characters is required."), "reason_required")
    now = timezone.now()
    WorkflowTask.objects.filter(instance=locked, status__in=OPEN_TASK_STATUSES).update(status=TaskStatus.CANCELLED, updated_at=now)
    locked.status, locked.cancelled_by, locked.cancelled_at, locked.cancel_reason = InstanceStatus.CANCELLED, actor, now, reason
    locked.revision += 1
    stamp_history(locked, actor, VERBS["cancelled"])
    locked.save()
    record_event(locked, EventKind.CANCELLED, actor, comment=reason, from_state=locked.state)
    log(VERBS["cancelled"], locked, actor, f"Cancelled {locked.reference}", reason=reason)
    events.send_on_commit(events.instance_finished, locked, actor)
    return locked
