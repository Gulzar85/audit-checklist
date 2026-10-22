"""Creating and editing assignments.  Authorization, validation, locking, history and audit all happen here, once."""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.services import apply_changes, log_activity, stamp_history
from apps.organizations.models import Organization

from .. import events, policies
from ..constants import AssignmentPriority, AssignmentStatus
from ..models import ChecklistAssignment
from . import validation as v
from .errors import AssignmentConflict, AssignmentError

VERBS = {
    "created": "assignment.created",
    "updated": "assignment.updated",
    "issued": "assignment.issued",
    "cancelled": "assignment.cancelled",
    "reopened": "assignment.reopened",
    "assignee_changed": "assignment.assignee_changed",
    "due_changed": "assignment.due_date_changed",
    "priority_changed": "assignment.priority_changed",
    "bulk_created": "assignment.bulk_created",
    "started": "assignment.started",
    "completed": "assignment.completed",
    "released": "assignment.released",
    "refused": "assignment.transition_refused",
}


def log(verb: str, assignment: ChecklistAssignment, actor: Any, description: str, **metadata: Any) -> None:
    """One audit entry; carries ``assignment_id`` so the assignment's timeline can find it.  No free text beyond the reason."""
    log_activity(
        verb=verb, organization=assignment.organization, actor=actor, target=assignment, description=description,
        metadata={"assignment_id": str(assignment.pk), "reference": assignment.reference, **metadata},
    )


def integrity_error(exc: IntegrityError) -> AssignmentError:
    """Map a database refusal to the user-facing rule it enforces (the services check first; this covers races)."""
    name = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", "") or ""
    if name == "one_open_assignment_per_version_location":
        return AssignmentConflict(_("Another open assignment for this checklist version and location was created at the same time."), "duplicate")
    if name.startswith("assignment_"):
        return AssignmentError(_("The assignment breaks an integrity rule (%(rule)s).") % {"rule": name}, name)
    return AssignmentError(_("The assignment conflicts with the current data. Reload and try again."), "conflict")


def lock_organization(organization_id: Any) -> Organization:
    """Serialise numbering (and concurrent creation) per organization."""
    return Organization.objects.select_for_update().get(pk=organization_id)


def next_number(organization_id: Any) -> int:
    top = ChecklistAssignment.objects.filter(organization_id=organization_id).order_by("-number").values_list("number", flat=True).first()
    return (top or 0) + 1


def _priority(value: Any) -> str:
    value = value or AssignmentPriority.NORMAL
    if value not in AssignmentPriority.values:
        raise AssignmentError(_("Unknown priority."), "invalid_priority")
    return value


def _instructions(text: Any) -> str:
    text = (text or "").strip()
    if len(text) > 2000:
        raise AssignmentError(_("Instructions may be at most 2000 characters."), "invalid_instructions")
    return text


@transaction.atomic
def create_assignment(
    *, actor: Any, version: Any, location: Any, priority: str = AssignmentPriority.NORMAL, due_at: Any = None,
    instructions: str = "", assigned_to: Any = None, issue: bool = False, number: int | None = None,
) -> ChecklistAssignment:
    """Create a draft assignment (``issue=False``) or create and issue it in one transaction."""
    organization = lock_organization(location.organization_id)
    location = type(location).objects.select_related("location_type", "organization").get(pk=location.pk)  # never trust a stale object
    v.require(policies.can_create_assignment(actor, location))
    v.require(policies.can_use_checklist(actor, organization))
    if issue:
        v.require(policies.can_issue_at(actor, location))
    v.validate_target(organization=organization, version=version, location=location)
    v.validate_due(due_at)
    v.validate_assignee(user=assigned_to, organization=organization, location=location)
    assignment = ChecklistAssignment(
        organization=organization, number=number or next_number(organization.pk), version=version, location=location,
        priority=_priority(priority), instructions=_instructions(instructions), assigned_to=assigned_to, due_at=due_at,
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
    )
    stamp_history(assignment, actor, VERBS["created"])
    try:
        with transaction.atomic():
            assignment.save()
    except IntegrityError as exc:
        raise integrity_error(exc) from exc
    log(VERBS["created"], assignment, actor, f"Created assignment {assignment.reference}",
        version=version.version_number, location=str(location.pk), priority=assignment.priority)
    if issue:
        from .lifecycle import issue_locked

        issue_locked(assignment, actor)
    return assignment


_DRAFT_FIELDS = ("version", "location", "priority", "due_at", "instructions", "assigned_to")


@transaction.atomic
def update_draft(assignment: ChecklistAssignment, *, actor: Any, **fields: Any) -> ChecklistAssignment:
    """Edit a draft (version, location, priority, due date, instructions, responsible user).  Issued assignments use the ``change_*`` operations."""
    locked = ChecklistAssignment.objects.select_for_update().select_related("organization", "version__template", "location").get(pk=assignment.pk)
    v.require(policies.can_edit_draft(actor, locked))
    if locked.status != AssignmentStatus.DRAFT:
        raise AssignmentError(_("Only a draft can be edited this way."), "not_draft")
    unknown = sorted(set(fields) - set(_DRAFT_FIELDS))
    if unknown:
        raise AssignmentError(_("These fields cannot be changed: %(f)s.") % {"f": ", ".join(unknown)}, "protected_field")
    new_version = fields.get("version", locked.version)
    new_location = fields.get("location", locked.location)
    if new_location.pk != locked.location_id:
        v.require(policies.can_create_assignment(actor, new_location))
    v.validate_target(organization=locked.organization, version=new_version, location=new_location)
    if "due_at" in fields:
        v.validate_due(fields["due_at"])
    assignee = fields.get("assigned_to", locked.assigned_to)
    v.validate_assignee(user=assignee, organization=locked.organization, location=new_location)
    data = dict(fields)
    if "priority" in data:
        data["priority"] = _priority(data["priority"])
    if "instructions" in data:
        data["instructions"] = _instructions(data["instructions"])
    changes = apply_changes(locked, data)
    if changes:
        stamp_history(locked, actor, VERBS["updated"])
        try:
            with transaction.atomic():
                locked.save()
        except IntegrityError as exc:
            raise integrity_error(exc) from exc
        log(VERBS["updated"], locked, actor, f"Updated draft {locked.reference}", changes=sorted(changes))
    return locked


def _change(assignment: ChecklistAssignment, actor: Any, field: str, value: Any, verb: str, signal: Any) -> ChecklistAssignment:
    locked = ChecklistAssignment.objects.select_for_update().select_related("organization", "location").get(pk=assignment.pk)
    v.require(policies.can_change_details(actor, locked))
    if field == "due_at":
        v.validate_due(value)
    elif field == "assigned_to":
        v.validate_assignee(user=value, organization=locked.organization, location=locked.location)
    else:
        value = _priority(value)
    changes = apply_changes(locked, {field: value})
    if changes:
        stamp_history(locked, actor, verb)
        locked.save()
        log(verb, locked, actor, f"{verb.split('.')[-1].replace('_', ' ').capitalize()} on {locked.reference}", change=changes[field])
        events.send_on_commit(signal, locked, actor, changes=changes)
    return locked


@transaction.atomic
def change_assignee(assignment: ChecklistAssignment, *, actor: Any, assigned_to: Any) -> ChecklistAssignment:
    """Designate (or clear) the responsible user of a draft or open assignment."""
    return _change(assignment, actor, "assigned_to", assigned_to, VERBS["assignee_changed"], events.assignment_assignee_changed)


@transaction.atomic
def change_due_date(assignment: ChecklistAssignment, *, actor: Any, due_at: Any) -> ChecklistAssignment:
    """Set, move or clear the due date; overdue presentation follows immediately (it is derived, not stored)."""
    return _change(assignment, actor, "due_at", due_at, VERBS["due_changed"], events.assignment_due_changed)


@transaction.atomic
def change_priority(assignment: ChecklistAssignment, *, actor: Any, priority: str) -> ChecklistAssignment:
    return _change(assignment, actor, "priority", priority, VERBS["priority_changed"], events.assignment_priority_changed)


__all__ = ["VERBS", "change_assignee", "change_due_date", "change_priority", "create_assignment", "log", "timezone", "update_draft"]


