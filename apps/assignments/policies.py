"""Authorization for assignments (ADR 0042/0043): organization membership, role permission and *location scope*.

Every decision calls the central ``apps.permissions.authorization`` service - no role name is tested here.  Assignment
permissions are **location-scoped**: ``has_permission(user, code, location=...)`` is true when the permission is held
organization-wide or through a grant on the location or one of its ancestors.  A forged location from another
organization is always refused by the central service.

Web views, the API, bulk assignment, admin and imports all call these functions (and the services, which call them again),
so there is one rule set.
"""

from __future__ import annotations

from typing import Any

from apps.core.policies import is_active_user
from apps.permissions.authorization import has_permission
from apps.permissions.registry import P

from .constants import AssignmentStatus


def _at(user: Any, code: str, location: Any) -> bool:
    return is_active_user(user) and location is not None and has_permission(user, code, location=location)


def can_view_assignment(user: Any, assignment: Any) -> bool:
    """Scope-based view, plus the designated responsible user may see their own (issued) assignment."""
    if _at(user, P.ASSIGNMENT_VIEW, assignment.location):
        return True
    return bool(is_active_user(user) and assignment.assigned_to_id == user.pk and assignment.status != AssignmentStatus.DRAFT)


def can_create_assignment(user: Any, location: Any) -> bool:
    return _at(user, P.ASSIGNMENT_CREATE, location)


def can_edit_draft(user: Any, assignment: Any) -> bool:
    return assignment.status == AssignmentStatus.DRAFT and _at(user, P.ASSIGNMENT_CREATE, assignment.location)


def can_issue(user: Any, assignment: Any) -> bool:
    return assignment.status == AssignmentStatus.DRAFT and _at(user, P.ASSIGNMENT_ISSUE, assignment.location)


def can_issue_at(user: Any, location: Any) -> bool:
    """May the user issue assignments at this location (status-independent; used before an assignment exists)?"""
    return _at(user, P.ASSIGNMENT_ISSUE, location)


def can_cancel_at(user: Any, location: Any) -> bool:
    return _at(user, P.ASSIGNMENT_CANCEL, location)


def can_reopen_at(user: Any, location: Any) -> bool:
    return _at(user, P.ASSIGNMENT_REOPEN, location)


def can_cancel(user: Any, assignment: Any) -> bool:
    return assignment.status in (AssignmentStatus.DRAFT, AssignmentStatus.ASSIGNED, AssignmentStatus.IN_PROGRESS) and _at(
        user, P.ASSIGNMENT_CANCEL, assignment.location)


def can_reopen(user: Any, assignment: Any) -> bool:
    return assignment.status == AssignmentStatus.CANCELLED and _at(user, P.ASSIGNMENT_REOPEN, assignment.location)


def can_change_details(user: Any, assignment: Any) -> bool:
    """Responsible user, due date and priority of a draft or open assignment."""
    return assignment.status in (AssignmentStatus.DRAFT, AssignmentStatus.ASSIGNED, AssignmentStatus.IN_PROGRESS) and _at(
        user, P.ASSIGNMENT_UPDATE, assignment.location)


def can_bulk_create(user: Any, location: Any) -> bool:
    return _at(user, P.ASSIGNMENT_BULK_CREATE, location)


def can_use_checklist(user: Any, organization: Any) -> bool:
    """May the user choose this organization's checklists (definitions are organization-level)?"""
    return is_active_user(user) and has_permission(user, P.CHECKLIST_VIEW, organization=organization)
