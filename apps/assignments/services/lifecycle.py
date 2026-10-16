"""The assignment lifecycle: the *only* place status changes (ADR 0051).

Persisted transitions (``constants.TRANSITIONS``)::

    draft ──issue──▶ assigned          draft/assigned/in_progress ──cancel──▶ cancelled          cancelled ──reopen──▶ draft

``in_progress`` and ``completed`` are reserved for Phase 9 (real executions): nothing here, in a form, a serializer, an import
or the admin can set them.  ``overdue`` is derived from ``due_at`` and the clock, never stored.

Repeat requests are safe: issuing an issued assignment or cancelling a cancelled one raises ``AssignmentConflict``
(``already_issued`` / ``already_cancelled``) and changes nothing.  A transition the table does not allow raises
``AssignmentError("invalid_transition")`` and is recorded as ``assignment.transition_refused`` (outside the rolled-back
transaction), so probing the lifecycle leaves an audit trail.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.services import stamp_history

from .. import events, policies
from ..constants import TRANSITIONS, AssignmentStatus
from ..models import ChecklistAssignment
from . import validation as v
from .assignment import VERBS, integrity_error, log
from .errors import AssignmentConflict, AssignmentError


def assert_transition(old: str, new: str) -> None:
    if new not in TRANSITIONS.get(old, frozenset()):
        raise AssignmentError(_("A %(old)s assignment cannot become %(new)s.") % {"old": old, "new": new}, "invalid_transition")


def _lock(assignment: ChecklistAssignment) -> ChecklistAssignment:
    return ChecklistAssignment.objects.select_for_update().select_related("organization", "version__template", "location").get(pk=assignment.pk)


def _refused(assignment: ChecklistAssignment, actor: Any, attempted: str, exc: Exception) -> None:
    fresh = ChecklistAssignment.objects.select_related("organization").get(pk=assignment.pk)
    log(VERBS["refused"], fresh, actor, f"Refused: {attempted} on {fresh.reference}", attempted=attempted, status=fresh.status,
        code=getattr(exc, "assignment_code", ""))


def _run(assignment: ChecklistAssignment, actor: Any, attempted: str, operation: Any) -> ChecklistAssignment:
    """Run one lifecycle operation; a refused *transition* is audited outside the rolled-back transaction."""
    try:
        return operation()
    except AssignmentError as exc:
        if exc.assignment_code in ("invalid_transition",):
            _refused(assignment, actor, attempted, exc)
        raise
    except PermissionDenied:
        raise


def issue_locked(assignment: ChecklistAssignment, actor: Any) -> ChecklistAssignment:
    """Issue ``assignment`` (row already locked by the caller): full revalidation, then draft -> assigned."""
    if assignment.status == AssignmentStatus.ASSIGNED:
        raise AssignmentConflict(_("%(ref)s is already issued; nothing was changed.") % {"ref": assignment.reference}, "already_issued")
    assert_transition(assignment.status, AssignmentStatus.ASSIGNED)
    v.require(policies.can_issue_at(actor, assignment.location))
    v.require(policies.can_use_checklist(actor, assignment.organization))
    v.validate_target(organization=assignment.organization, version=assignment.version, location=assignment.location)
    v.validate_due(assignment.due_at)
    v.validate_assignee(user=assignment.assigned_to, organization=assignment.organization, location=assignment.location)
    v.check_duplicate(assignment.version, assignment.location, exclude=assignment)
    assignment.status = AssignmentStatus.ASSIGNED
    assignment.issued_at = timezone.now()
    assignment.issued_by = actor if getattr(actor, "is_authenticated", False) else None
    stamp_history(assignment, actor, VERBS["issued"])
    try:
        with transaction.atomic():
            assignment.save()
    except IntegrityError as exc:
        raise integrity_error(exc) from exc
    log(VERBS["issued"], assignment, actor, f"Issued {assignment.reference}", version=assignment.version.version_number,
        location=str(assignment.location_id), due_at=assignment.due_at.isoformat() if assignment.due_at else None)
    events.send_on_commit(events.assignment_issued, assignment, actor)
    return assignment


def issue(assignment: ChecklistAssignment, *, actor: Any) -> ChecklistAssignment:
    """Issue a draft assignment to its location."""
    def operation() -> ChecklistAssignment:
        with transaction.atomic():
            return issue_locked(_lock(assignment), actor)

    return _run(assignment, actor, "issue", operation)


def cancel(assignment: ChecklistAssignment, *, actor: Any, reason: str) -> ChecklistAssignment:
    """Cancel a draft or open assignment; a reason is required and recorded."""
    reason = (reason or "").strip()

    def operation() -> ChecklistAssignment:
        with transaction.atomic():
            locked = _lock(assignment)
            if locked.status == AssignmentStatus.CANCELLED:
                raise AssignmentConflict(_("%(ref)s is already cancelled; nothing was changed.") % {"ref": locked.reference}, "already_cancelled")
            v.require(policies.can_cancel_at(actor, locked.location))  # scope first, then the transition rules
            assert_transition(locked.status, AssignmentStatus.CANCELLED)
            if not reason:
                raise AssignmentError(_("A cancellation reason is required."), "reason_required")
            if len(reason) > 300:
                raise AssignmentError(_("The reason may be at most 300 characters."), "reason_required")
            previous = locked.status
            locked.status = AssignmentStatus.CANCELLED
            locked.cancelled_at = timezone.now()
            locked.cancelled_by = actor if getattr(actor, "is_authenticated", False) else None
            locked.cancel_reason = reason
            stamp_history(locked, actor, VERBS["cancelled"])
            locked.save()
            log(VERBS["cancelled"], locked, actor, f"Cancelled {locked.reference}", previous_status=previous, reason=reason)
            events.send_on_commit(events.assignment_cancelled, locked, actor, reason=reason)
            return locked

    return _run(assignment, actor, "cancel", operation)


def reopen(assignment: ChecklistAssignment, *, actor: Any) -> ChecklistAssignment:
    """Reopen a cancelled assignment *as a draft* (explicit and authorized); issuing it again revalidates everything."""
    def operation() -> ChecklistAssignment:
        with transaction.atomic():
            locked = _lock(assignment)
            v.require(policies.can_reopen_at(actor, locked.location))
            assert_transition(locked.status, AssignmentStatus.DRAFT)
            locked.status = AssignmentStatus.DRAFT
            locked.issued_at = locked.issued_by = locked.cancelled_at = locked.cancelled_by = None
            locked.cancel_reason = ""
            stamp_history(locked, actor, VERBS["reopened"])
            locked.save()
            log(VERBS["reopened"], locked, actor, f"Reopened {locked.reference} as a draft")
            events.send_on_commit(events.assignment_reopened, locked, actor)
            return locked

    return _run(assignment, actor, "reopen", operation)
