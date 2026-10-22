"""Execution lifecycle: start/resume and cancel.  Submission lives in ``submission.py``; saving in ``responses.py``.

Lock order (always): organization (numbering only) -> assignment -> execution.  Saves lock only the execution.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.assignments.models import ChecklistAssignment
from apps.assignments.services import lifecycle as assignment_lifecycle
from apps.checklists.manifest import verify_version_integrity
from apps.core.services import stamp_history

from .. import events, policies
from ..constants import LIVE_STATUSES, ExecutionStatus
from ..models import ChecklistExecution
from .base import VERBS, integrity_error, lock_execution, lock_organization, log, next_number
from .errors import ExecutionConflict, ExecutionError


def _lock_assignment(assignment_id: Any) -> ChecklistAssignment:
    return (ChecklistAssignment.objects.select_for_update(of=("self",))
            .select_related("organization", "location", "version__template").get(pk=assignment_id))


@transaction.atomic
def start_or_resume(assignment: ChecklistAssignment, *, actor: Any) -> tuple[ChecklistExecution, bool]:
    """The assignment's live execution, created on first use: ``(execution, created)``.

    Idempotent: a repeated start returns the existing in-progress execution (or the submitted one - a new execution is never
    created after submission).  Authorization (403) precedes every state check.
    """
    locked = _lock_assignment(assignment.pk)
    if not policies.can_resume_at(actor, locked):
        raise PermissionDenied
    existing = ChecklistExecution.objects.filter(assignment=locked, status__in=LIVE_STATUSES).select_related(
        "organization", "version__template").first()
    if existing is not None:
        existing.assignment = locked
        return existing, False
    if locked.status not in policies.EXECUTABLE_ASSIGNMENT_STATUSES:
        raise ExecutionError(_("This assignment is %(status)s and is not open for work.") % {"status": locked.get_status_display().lower()},
                             "not_executable")
    version = locked.version
    if verify_version_integrity(version).status == "mismatch":
        raise ExecutionError(_("The checklist version no longer matches what was published; it cannot be executed."), "integrity")
    lock_organization(locked.organization_id)
    execution = ChecklistExecution(
        organization=locked.organization, number=next_number(locked.organization_id), assignment=locked, version=version,
        started_by=actor, started_at=timezone.now(), last_activity_at=timezone.now(),
    )
    stamp_history(execution, actor, VERBS["started"])
    try:
        with transaction.atomic():
            execution.save()
    except IntegrityError as exc:
        raise integrity_error(exc) from exc
    assignment_lifecycle.mark_in_progress(locked, actor=actor)
    log(VERBS["started"], execution, actor, f"Started {execution.reference} for {locked.reference}",
        version=version.version_number, location=str(locked.location_id))
    events.send_on_commit(events.execution_started, execution, actor)
    return execution, True


def _cancel_locked(execution: ChecklistExecution, actor: Any, reason: str) -> None:
    execution.status = ExecutionStatus.CANCELLED
    execution.cancelled_at = timezone.now()
    execution.cancelled_by = actor if getattr(actor, "is_authenticated", False) else None
    execution.cancel_reason = reason[:300]
    execution.revision += 1
    stamp_history(execution, actor, VERBS["cancelled"])
    execution.save()
    log(VERBS["cancelled"], execution, actor, f"Cancelled {execution.reference}", reason=execution.cancel_reason)
    events.send_on_commit(events.execution_cancelled, execution, actor, reason=execution.cancel_reason)


@transaction.atomic
def cancel_execution(execution: ChecklistExecution, *, actor: Any, reason: str) -> ChecklistExecution:
    """Cancel an in-progress execution (its answers are kept); the assignment becomes open again."""
    reason = (reason or "").strip()
    assignment = _lock_assignment(execution.assignment_id)
    locked = lock_execution(execution.pk)
    locked.assignment = assignment
    if not policies.can_cancel_at(actor, locked):
        raise PermissionDenied
    if locked.status == ExecutionStatus.CANCELLED:
        raise ExecutionConflict(_("%(ref)s is already cancelled; nothing was changed.") % {"ref": locked.reference}, "already_cancelled")
    if locked.status == ExecutionStatus.SUBMITTED:
        raise ExecutionConflict(_("%(ref)s was submitted and cannot be cancelled.") % {"ref": locked.reference}, "already_submitted")
    if not reason:
        raise ExecutionError(_("A cancellation reason is required."), "reason_required")
    if len(reason) > 300:
        raise ExecutionError(_("The reason may be at most 300 characters."), "reason_required")
    _cancel_locked(locked, actor, reason)
    assignment_lifecycle.mark_released(assignment, actor=actor)
    return locked


def cancel_for_assignment(assignment: ChecklistAssignment, actor: Any, reason: str) -> None:
    """Hook run inside ``assignments.lifecycle.cancel`` (assignment row locked): cancel its in-progress execution, keep its answers."""
    live = (ChecklistExecution.objects.select_for_update(of=("self",)).select_related("organization")
            .filter(assignment=assignment, status=ExecutionStatus.IN_PROGRESS).first())
    if live is not None:
        live.assignment = assignment
        _cancel_locked(live, actor, f"Assignment cancelled: {reason}")
