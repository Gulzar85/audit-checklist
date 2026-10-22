"""Authorization for executions (ADR 0042/0043): membership, role permission, *location scope* and assignment eligibility.

Every decision calls the central ``apps.permissions.authorization`` service - no role name is tested here.  Running an
assignment needs ``execution.create`` / ``execution.update`` / ``execution.submit`` at the assignment's location *and* the
assignment itself to allow that user: an assignment designated to someone is theirs alone; an undesignated one is open to the
location's eligible users.  Viewing a checklist definition never grants the right to execute.
"""

from __future__ import annotations

from typing import Any

from apps.assignments.constants import AssignmentStatus
from apps.core.policies import is_active_user
from apps.permissions.authorization import has_permission
from apps.permissions.registry import P

from .constants import ExecutionStatus

EXECUTABLE_ASSIGNMENT_STATUSES = (AssignmentStatus.ASSIGNED, AssignmentStatus.IN_PROGRESS)


def _at(user: Any, code: str, location: Any) -> bool:
    return is_active_user(user) and location is not None and has_permission(user, code, location=location)


def is_designee_or_open(user: Any, assignment: Any) -> bool:
    """An assignment is for its designated user; without a designee it is open to the location's eligible users."""
    return assignment.assigned_to_id is None or assignment.assigned_to_id == getattr(user, "pk", None)


def can_start(user: Any, assignment: Any) -> bool:
    return (assignment.status in EXECUTABLE_ASSIGNMENT_STATUSES and is_designee_or_open(user, assignment)
            and _at(user, P.EXECUTION_CREATE, assignment.location))


def can_resume_at(user: Any, assignment: Any) -> bool:
    """Scope + designee check without the assignment-status rule (used before the status is read under lock)."""
    return is_designee_or_open(user, assignment) and _at(user, P.EXECUTION_CREATE, assignment.location)


def can_work(user: Any, execution: Any) -> bool:
    """Answer questions and save drafts."""
    return (execution.status == ExecutionStatus.IN_PROGRESS and is_designee_or_open(user, execution.assignment)
            and _at(user, P.EXECUTION_UPDATE, execution.assignment.location))


def can_work_at(user: Any, execution: Any) -> bool:
    """Scope + designee check without the execution-status rule (so a state error can be reported after authorization)."""
    return is_designee_or_open(user, execution.assignment) and _at(user, P.EXECUTION_UPDATE, execution.assignment.location)


def can_submit(user: Any, execution: Any) -> bool:
    return (is_designee_or_open(user, execution.assignment) and _at(user, P.EXECUTION_SUBMIT, execution.assignment.location))


def can_cancel(user: Any, execution: Any) -> bool:
    return execution.status == ExecutionStatus.IN_PROGRESS and _at(user, P.EXECUTION_CANCEL, execution.assignment.location)


def can_cancel_at(user: Any, execution: Any) -> bool:
    return _at(user, P.EXECUTION_CANCEL, execution.assignment.location)


def can_view(user: Any, execution: Any) -> bool:
    """Scope-based view, plus the people who did or were designated to do the work."""
    if _at(user, P.EXECUTION_VIEW, execution.assignment.location):
        return True
    return bool(is_active_user(user) and user.pk in (execution.started_by_id, execution.assignment.assigned_to_id))
