"""Submission (``ExecutionSubmissionService``): one transaction, concurrency-safe and idempotent.

Lock order: assignment -> execution (the same as the lifecycle), so a submission waits for an in-flight save and a later save sees
a submitted execution.  Everything is revalidated from the stored responses - the client sends no answers, no counts, no state.
If anything blocks submission nothing changes (the refusal is audited *outside* the rolled-back transaction); if it succeeds the
execution becomes read-only (also at the database: ``guards.py``), rule-hidden answers are stamped ``is_hidden`` and the
assignment is completed through its own lifecycle service.  A second submission is reported, not repeated: no second event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.assignments.constants import AssignmentStatus
from apps.assignments.services import lifecycle as assignment_lifecycle
from apps.core.services import stamp_history

from .. import events, policies
from ..constants import ExecutionStatus
from ..models import ChecklistExecution, ChecklistResponse
from .base import VERBS, integrity_error, lock_execution, log
from .errors import ExecutionConflict, ExecutionError, ExecutionValidationError
from .evaluation import ExecutionState, build_state
from .lifecycle import _lock_assignment
from .validation import describe, submission_problems


@dataclass
class SubmissionResult:
    execution: ChecklistExecution
    already_submitted: bool = False


def review(execution: ChecklistExecution) -> tuple[ExecutionState, dict[str, list[dict[str, str]]]]:
    """The server-side pre-submit review: the current state and everything that would block submission (read-only)."""
    state = build_state(execution)
    return state, submission_problems(state)


def submit(execution: ChecklistExecution, *, actor: Any) -> SubmissionResult:
    try:
        return _submit(execution, actor)
    except ExecutionValidationError as exc:
        fresh = ChecklistExecution.objects.select_related("organization").get(pk=execution.pk)
        log(VERBS["refused"], fresh, actor, f"Submission of {fresh.reference} refused", items=sorted(k for k in exc.problems if k)[:50],
            count=sum(len(v) for v in exc.problems.values()))
        raise


@transaction.atomic
def _submit(execution: ChecklistExecution, actor: Any) -> SubmissionResult:
    assignment = _lock_assignment(execution.assignment_id)
    locked = lock_execution(execution.pk)
    locked.assignment = assignment
    if not policies.can_submit(actor, locked):
        raise PermissionDenied
    if locked.status == ExecutionStatus.SUBMITTED:
        return SubmissionResult(locked, already_submitted=True)
    if locked.status != ExecutionStatus.IN_PROGRESS:
        raise ExecutionConflict(_("This execution was cancelled and cannot be submitted."), "not_editable")
    if assignment.status != AssignmentStatus.IN_PROGRESS or assignment.version_id != locked.version_id:
        raise ExecutionError(_("The assignment is no longer open for this execution."), "not_executable")
    state = build_state(locked)
    problems = submission_problems(state)
    if problems:
        raise ExecutionValidationError(problems, "The checklist cannot be submitted until the problems below are fixed.")
    hidden_ids = [state.snapshot.items[code].id for code, r in state.evaluation.items.items() if not r.visible and code in state.responses]
    if hidden_ids:  # kept for audit, ignored from now on; done while the execution is still in progress (the guard allows it)
        ChecklistResponse.objects.filter(execution=locked, item_id__in=hidden_ids).update(is_hidden=True)
    now = timezone.now()
    locked.status = ExecutionStatus.SUBMITTED
    locked.submitted_at, locked.submitted_by = now, actor
    locked.last_activity_at = now
    locked.revision += 1
    stamp_history(locked, actor, VERBS["submitted"])
    try:
        with transaction.atomic():
            locked.save()
    except IntegrityError as exc:
        raise integrity_error(exc) from exc
    assignment_lifecycle.mark_completed(assignment, actor=actor)
    from apps.findings.services import detect_after_submission  # local import: findings depend on executions, not the reverse

    findings = detect_after_submission(locked, actor)  # Phase 12: raises the findings the rules call for, inside this transaction
    log(VERBS["submitted"], locked, actor, f"Submitted {locked.reference}", answers=len(state.responses) - len(hidden_ids), hidden=len(hidden_ids),
        version=locked.version.version_number, findings_raised=len(findings.created) if findings else 0)
    events.send_on_commit(events.execution_submitted, locked, actor)
    return SubmissionResult(locked)


__all__ = ["SubmissionResult", "describe", "review", "submit"]
