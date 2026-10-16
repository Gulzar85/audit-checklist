"""Shared validation: the same checks for a draft, an issue, a change, a bulk target, the API and the UI."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.checklists import lifecycle as checklist_lifecycle
from apps.checklists.manifest import verify_version_integrity
from apps.permissions.authorization import has_permission
from apps.permissions.registry import P

from .. import policies
from ..constants import OPEN_STATUSES, AssignmentStatus
from ..models import ChecklistAssignment
from .errors import AssignmentConflict, AssignmentError


def require(allowed: bool) -> None:
    """Authorization failures are ``PermissionDenied`` (HTTP 403); callers fetch objects through selectors first (404)."""
    if not allowed:
        raise PermissionDenied


def validate_due(due_at: Any, *, now: Any = None) -> None:
    """A due date is optional (no date = no deadline, never overdue); when given it must be a future moment."""
    if due_at is None:
        return
    if timezone.is_naive(due_at):
        raise AssignmentError(_("The due date must include a time zone."), "invalid_due")
    if due_at <= (now or timezone.now()):
        raise AssignmentError(_("The due date must be in the future."), "invalid_due")


def validate_target(*, organization: Any, version: Any, location: Any) -> None:
    """Version and location are eligible for a *new* assignment in ``organization`` (published, current, same organization)."""
    if version.template.organization_id != organization.pk:
        raise AssignmentError(_("The checklist version does not belong to this organization."), "foreign_version")
    if location.organization_id != organization.pk:
        raise AssignmentError(_("The location does not belong to this organization."), "foreign_location")
    if not location.is_active:
        raise AssignmentError(_("The location is inactive and cannot receive new assignments."), "inactive_location")
    try:
        checklist_lifecycle.assert_assignable(version)  # published, current, active template, active organization
    except Exception as exc:  # LifecycleError (a ValidationError): re-raise as an assignment error with the same reason
        raise AssignmentError(exc.messages, getattr(exc, "lifecycle_code", "version_not_assignable")) from exc
    if verify_version_integrity(version).status == "mismatch":
        raise AssignmentError(_("The checklist version no longer matches what was published and cannot be assigned."), "integrity")


def validate_assignee(*, user: Any, organization: Any, location: Any) -> None:
    """The responsible user is active, an active member of the organization and allowed to run checklists at the location."""
    if user is None:
        return
    from apps.organizations.models import OrganizationMembership

    if not user.is_active or not OrganizationMembership.objects.filter(user=user, organization=organization, is_active=True).exists():
        raise AssignmentError(_("The responsible user is not an active member of this organization."), "invalid_assignee")
    if not has_permission(user, P.EXECUTION_CREATE, organization=organization, location=location):
        raise AssignmentError(_("The responsible user is not allowed to complete checklists at this location."), "invalid_assignee")


def open_duplicates(version: Any, location: Any, *, exclude: Any = None, include_drafts: bool = False):  # noqa: ANN201
    statuses = [*OPEN_STATUSES, *( [AssignmentStatus.DRAFT] if include_drafts else [])]
    queryset = ChecklistAssignment.objects.filter(version=version, location=location, status__in=statuses)
    return queryset.exclude(pk=exclude.pk) if exclude is not None else queryset


def check_duplicate(version: Any, location: Any, *, exclude: Any = None, include_drafts: bool = False) -> None:
    """Duplicate policy (ADR 0051): one *open* assignment per exact version and location."""
    existing = open_duplicates(version, location, exclude=exclude, include_drafts=include_drafts).first()
    if existing is not None:
        raise AssignmentConflict(
            _("%(ref)s already assigns this checklist version to this location. Cancel it first, or assign a newer version.")
            % {"ref": existing.reference}, "duplicate",
        )


def can_act_on(actor: Any, location: Any, check: Any) -> None:
    require(check(actor, location))


__all__ = ["check_duplicate", "open_duplicates", "policies", "require", "validate_assignee", "validate_due", "validate_target"]
