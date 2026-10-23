"""Manual creation, controlled edits, assignment and the lifecycle transitions (``FindingLifecycleService``).

Every status change goes through ``transition``: authorization (403) first, then state (409 for a repeat, 400 for an invalid move),
required fields, the closure policy, then one locked write with history and audit.  Nothing else - no serializer, form, import or admin
- writes ``status``.  Lock order: organization (numbering only) -> finding.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.services import apply_changes, stamp_history
from apps.permissions.registry import P

from .. import events, hooks, policies
from ..constants import EDITABLE_STATUSES, TRANSITIONS, FindingStatus, SourceType
from ..models import Finding, FindingCategory, FindingSeverity
from . import config
from .base import VERBS, integrity_error, lock_finding, lock_organization, log, next_number
from .errors import FindingConflict, FindingError

UNSET = object()
MAX_TITLE, MAX_DESCRIPTION, MAX_REASON, MAX_SUMMARY = 200, 2000, 300, 1000


def _text(value: Any, label: str, maximum: int, *, required: bool = False) -> str:
    text = (value or "").strip() if isinstance(value, str) or value is None else None
    if text is None:
        raise FindingError(_("%(label)s must be text.") % {"label": label}, "invalid_text")
    if required and not text:
        raise FindingError(_("%(label)s is required.") % {"label": label}, "required_field")
    if len(text) > maximum:
        raise FindingError(_("%(label)s may be at most %(n)d characters.") % {"label": label, "n": maximum}, "too_long")
    return text


def _due(value: Any) -> Any:
    if value is None:
        return None
    if timezone.is_naive(value):
        raise FindingError(_("The due date needs a time zone."), "invalid_due")
    if value <= timezone.now():
        raise FindingError(_("The due date must be in the future."), "invalid_due")
    return value


def _classification(organization: Any, category: Any, severity: Any) -> tuple[FindingCategory, FindingSeverity]:
    """The category and severity must be *active and in this organization* (a client can name an id from anywhere)."""
    if not isinstance(category, FindingCategory) or category.organization_id != organization.pk or not category.is_active:
        raise FindingError(_("Choose an active category of this organization."), "invalid_category")
    if not isinstance(severity, FindingSeverity) or severity.organization_id != organization.pk or not severity.is_active:
        raise FindingError(_("Choose an active severity level of this organization."), "invalid_severity")
    return category, severity


def _assignee(user: Any, location: Any) -> Any:
    if user is None:
        return None
    if not policies.can_be_assigned(user, location):
        raise FindingError(_("That user cannot be made responsible for findings at this location."), "invalid_assignee")
    return user


# ---------------------------------------------------------------------------
@transaction.atomic
def create_manual(*, actor: Any, location: Any, title: str, description: str = "", category: Any, severity: Any, execution: Any = None, item: Any = None,
                  assigned_to: Any = None, due_at: Any = None) -> Finding:
    """An authorized manual finding.  The creator is always the actor; the source is ``manual``; relationships are validated server-side."""
    if not policies.can_create_at(actor, location):
        raise PermissionDenied
    organization = location.organization
    category, severity = _classification(organization, category, severity)
    title = _text(title, _("The title"), MAX_TITLE, required=True)
    description = _text(description, _("The description"), MAX_DESCRIPTION)
    assignment = version = response = None
    if execution is not None:
        from apps.executions import selectors as exec_selectors
        from apps.executions.constants import ExecutionStatus

        visible = exec_selectors.get_execution(actor, execution.pk)
        if visible is None or visible.organization_id != organization.pk:
            raise FindingError(_("Unknown checklist execution."), "unknown_source")
        if visible.status == ExecutionStatus.CANCELLED:
            raise FindingError(_("A cancelled execution cannot be the source of a finding."), "invalid_source")
        if visible.assignment.location_id != location.pk:
            raise FindingError(_("The execution was not carried out at this location."), "invalid_source")
        execution, assignment, version = visible, visible.assignment, visible.version
        if item is not None:
            from apps.checklists.models import ChecklistItem

            if not ChecklistItem.objects.filter(pk=item.pk, section__version_id=version.pk).exists():
                raise FindingError(_("That question does not belong to the execution's checklist version."), "invalid_source")
            from apps.executions.models import ChecklistResponse

            response = ChecklistResponse.objects.filter(execution=execution, item=item).first()
    elif item is not None:
        raise FindingError(_("A question can only be linked together with its execution."), "invalid_source")
    owner = _assignee(assigned_to, location)
    if owner is None and severity.requires_owner:
        raise FindingError(_("A responsible user is required for %(severity)s findings.") % {"severity": severity.name}, "owner_required")
    detected_at = timezone.now()
    due = _due(due_at)
    if due is None and severity.default_due_days:
        due = detected_at + timedelta(days=severity.default_due_days)
    lock_organization(organization.pk)
    finding = Finding(
        organization=organization, number=next_number(organization.pk), title=title, description=description, category=category, severity=severity,
        source_type=SourceType.MANUAL, location=location, assignment=assignment, execution=execution, version=version, item=item, response=response,
        detected_at=detected_at, detected_by=actor, assigned_to=owner, due_at=due,
    )
    stamp_history(finding, actor, VERBS["created"])
    try:
        with transaction.atomic():
            finding.save()
    except IntegrityError as exc:
        raise integrity_error(exc) from exc
    log(VERBS["created"], finding, actor, f"Created {finding.reference}: {finding.title}", source=SourceType.MANUAL, severity=severity.code,
        category=category.code, location=str(location.pk), execution_id=str(execution.pk) if execution else None)
    events.send_on_commit(events.finding_raised, finding, actor, source=SourceType.MANUAL)
    return finding


# ---------------------------------------------------------------------------
@transaction.atomic
def update_finding(finding: Finding, *, actor: Any, title: Any = UNSET, description: Any = UNSET, category: Any = UNSET, severity: Any = UNSET,
                   due_at: Any = UNSET) -> Finding:
    """Edit the *triage* fields only.  Source traceability, status, ownership and detection metadata are not reachable from here."""
    locked = lock_finding(finding.pk)
    if not policies.can_update(actor, locked):
        if locked.status not in EDITABLE_STATUSES and policies._at(actor, P.FINDING_UPDATE, locked.location):
            raise FindingConflict(_("A %(status)s finding can no longer be edited.") % {"status": locked.get_status_display().lower()}, "not_editable")
        raise PermissionDenied
    data: dict[str, Any] = {}
    if title is not UNSET:
        data["title"] = _text(title, _("The title"), MAX_TITLE, required=True)
    if description is not UNSET:
        data["description"] = _text(description, _("The description"), MAX_DESCRIPTION)
    if category is not UNSET or severity is not UNSET:
        new_category, new_severity = _classification(
            locked.organization, locked.category if category is UNSET else category, locked.severity if severity is UNSET else severity)
        if category is not UNSET and new_category.pk != locked.category_id:
            data["category"] = new_category
        if severity is not UNSET and new_severity.pk != locked.severity_id:
            data["severity"] = new_severity
    if due_at is not UNSET:
        due = _due(due_at) if due_at != locked.due_at else due_at
        data["due_at"] = due
    changes = apply_changes(locked, data)
    if not changes:
        return locked
    stamp_history(locked, actor, VERBS["updated"])
    locked.save()
    verb = VERBS["updated"]
    log(verb, locked, actor, f"Updated {locked.reference}", changes=changes)
    if "severity" in changes:
        log(VERBS["severity_changed"], locked, actor, f"Severity of {locked.reference} changed to {locked.severity.name}", changes=changes["severity"])
    if "category" in changes:
        log(VERBS["category_changed"], locked, actor, f"Category of {locked.reference} changed to {locked.category.name}", changes=changes["category"])
    if "due_at" in changes:
        log(VERBS["due_changed"], locked, actor, f"Due date of {locked.reference} changed", changes=changes["due_at"])
    events.send_on_commit(events.finding_updated, locked, actor, changes=changes)
    return locked


@transaction.atomic
def assign(finding: Finding, *, actor: Any, assigned_to: Any, due_at: Any = UNSET) -> Finding:
    """Set (or clear) the responsible user and optionally the due date.  The assignee must be eligible at the finding's location."""
    locked = lock_finding(finding.pk)
    if not policies.can_assign(actor, locked):
        if locked.status not in EDITABLE_STATUSES and policies._at(actor, P.FINDING_ASSIGN, locked.location):
            raise FindingConflict(_("A %(status)s finding can no longer be reassigned.") % {"status": locked.get_status_display().lower()}, "not_editable")
        raise PermissionDenied
    owner = _assignee(assigned_to, locked.location)
    if owner is None and locked.severity.requires_owner and locked.status in (FindingStatus.IN_PROGRESS,):
        raise FindingError(_("Work is in progress and %(severity)s findings need a responsible user.") % {"severity": locked.severity.name}, "owner_required")
    data: dict[str, Any] = {"assigned_to": owner}
    if due_at is not UNSET:
        data["due_at"] = _due(due_at) if due_at != locked.due_at else due_at
    changes = apply_changes(locked, data)
    if not changes:
        return locked
    stamp_history(locked, actor, VERBS["assigned"])
    locked.save()
    log(VERBS["assigned"], locked, actor, f"{locked.reference} assigned to {owner.get_short_name() if owner else 'no one'}", changes=changes)
    events.send_on_commit(events.finding_assigned, locked, actor, changes=changes)
    return locked


# ---------------------------------------------------------------------------
def _closure_problem(finding: Finding) -> str | None:
    for blocker in hooks.closure_blockers:
        message = blocker(finding)
        if message:
            return str(message)
    if getattr(settings, "FINDINGS_REQUIRE_VERIFICATION", False) and not any(gate(finding) for gate in hooks.verification_gate):
        return str(_("Closing requires an independent verification, which is not available yet. The finding stays resolved."))
    return None


def _blocker_problem(finding: Finding, blockers: list) -> str | None:
    for blocker in blockers:
        message = blocker(finding)
        if message:
            return str(message)
    return None


def transition(finding: Finding, action: str, *, actor: Any, reason: str = "") -> Finding:
    try:
        return _transition(finding, action, actor, reason)
    except FindingError as exc:
        fresh = Finding.objects.select_related("organization").get(pk=finding.pk)
        log(VERBS["refused"], fresh, actor, f"{action} on {fresh.reference} refused", action=action, code=exc.finding_code)
        raise


@transaction.atomic
def _transition(finding: Finding, action: str, actor: Any, reason: str) -> Finding:
    spec = TRANSITIONS.get(action)
    if spec is None:
        raise FindingError(_("Unknown action."), "unknown_action")
    locked = lock_finding(finding.pk)
    if not policies.can_act(actor, locked, action):
        raise PermissionDenied
    if locked.status == spec.target:
        raise FindingConflict(_("%(ref)s is already %(status)s; nothing was changed.") % {"ref": locked.reference, "status": locked.get_status_display().lower()},
                              "already_" + spec.target)
    if locked.status not in spec.sources:
        raise FindingError(_("%(action)s is not available for a %(status)s finding.") % {"action": spec.label, "status": locked.get_status_display().lower()}, "invalid_transition")
    reason = (reason or "").strip()
    if spec.reason_required and not reason:
        raise FindingError(_("A reason is required.") if action != "resolve" else _("A resolution summary is required."), "reason_required")
    if len(reason) > (MAX_SUMMARY if action == "resolve" else MAX_REASON):
        raise FindingError(_("The text is too long."), "too_long")
    previous, now = locked.status, timezone.now()
    extra: dict[str, Any] = {}
    if action == "start" and locked.severity.requires_owner and locked.assigned_to_id is None:
        raise FindingError(_("Assign a responsible user before work starts on a %(severity)s finding.") % {"severity": locked.severity.name}, "owner_required")
    if action in ("resolve", "cancel"):
        problem = _blocker_problem(locked, hooks.resolve_blockers if action == "resolve" else hooks.cancel_blockers)
        if problem:
            raise FindingError(problem, "resolution_blocked" if action == "resolve" else "cancellation_blocked")
    if action == "resolve":
        locked.resolution_summary, locked.resolved_by, locked.resolved_at = reason, actor, now
    elif action == "close":
        problem = _closure_problem(locked)
        if problem:
            raise FindingError(problem, "closure_blocked")
        locked.closed_by, locked.closed_at = actor, now
    elif action == "reopen":
        extra["previous_resolution"] = locked.resolution_summary
        locked.reopen_count += 1
        locked.closed_by = locked.closed_at = locked.resolved_by = locked.resolved_at = None
    elif action == "cancel":
        locked.cancelled_by, locked.cancelled_at, locked.cancel_reason = actor, now, reason
    locked.status = spec.target
    stamp_history(locked, actor, VERBS[action])
    try:
        with transaction.atomic():
            locked.save()
    except IntegrityError as exc:
        raise integrity_error(exc) from exc
    log(VERBS[action], locked, actor, f"{spec.label} {locked.reference}", reason=reason, previous_status=previous, status=locked.status, **extra)
    events.send_on_commit(events.finding_status_changed, locked, actor, action=action, previous_status=previous)
    return locked
