"""Creation, planning edits, assignment, the work log and the lifecycle transitions (``CorrectiveActionLifecycleService``).

Every status change goes through ``transition``: authorization (403, including ownership and separation of duties) -> already in the
requested state (409, nothing changed) -> invalid move (400) -> required text and action-specific rules (evidence, owner) -> one locked
write with history, audit and an after-commit signal.  Nothing else - no serializer, form, import or admin - writes ``status``.
Lock order: organization (numbering only) -> action.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.services import apply_changes, stamp_history
from apps.permissions.registry import P

from .. import events, hooks, policies, selectors
from ..constants import PLANNING_STATUSES, TRANSITIONS, ActionStatus, Decision, Priority, UpdateKind
from ..models import CorrectiveAction, CorrectiveActionUpdate, CorrectiveActionVerification
from .base import VERBS, integrity_error, lock_action, lock_organization, log, next_number
from .errors import ActionConflict, ActionError

UNSET = object()
LIMITS = {"title": 200, "description": 2000, "plan": 2000, "expected_outcome": 500, "completion_criteria": 500}
WORKER_FIELDS = ("plan", "expected_outcome", "completion_criteria")
MANAGER_FIELDS = ("title", "description", "priority", "due_at", "required_evidence")
MAX_UPDATE, MAX_BLOCKER, MAX_REASON, MAX_SUMMARY, MAX_NOTES = 2000, 500, 300, 1000, 1000


def _text(value: Any, label: str, maximum: int, *, required: bool = False) -> str:
    text = (value or "").strip() if isinstance(value, str) or value is None else None
    if text is None:
        raise ActionError(_("%(label)s must be text.") % {"label": label}, "invalid_text")
    if required and not text:
        raise ActionError(_("%(label)s is required.") % {"label": label}, "required_field")
    if len(text) > maximum:
        raise ActionError(_("%(label)s may be at most %(n)d characters.") % {"label": label, "n": maximum}, "too_long")
    return text


def _due(value: Any) -> Any:
    if value is None:
        return None
    if timezone.is_naive(value):
        raise ActionError(_("The due date needs a time zone."), "invalid_due")
    if value <= timezone.now():
        raise ActionError(_("The due date must be in the future."), "invalid_due")
    return value


def _priority(value: Any) -> str:
    value = value or Priority.NORMAL
    if value not in Priority.values:
        raise ActionError(_("Unknown priority."), "invalid_priority")
    return value


def _required_evidence(value: Any) -> int:
    if value is None:
        value = int(getattr(settings, "CORRECTIVE_ACTION_DEFAULT_REQUIRED_EVIDENCE", 0))
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10:
        raise ActionError(_("The number of required files must be a whole number from 0 to 10."), "invalid_evidence_requirement")
    return value


def _assignee(user: Any, location: Any) -> Any:
    if user is None:
        return None
    if not policies.can_be_assigned(user, location):
        raise ActionError(_("That user cannot be made responsible for corrective actions at this location."), "invalid_assignee")
    return user


# ---------------------------------------------------------------------------
@transaction.atomic
def create_action(*, actor: Any, finding: Any, title: str, description: str = "", plan: str = "", expected_outcome: str = "", completion_criteria: str = "",
                  priority: str = "", assigned_to: Any = None, due_at: Any = None, required_evidence: Any = None, issue: bool = False) -> CorrectiveAction:
    """A new action against a finding.  Organization and location are the finding's; the creator is the actor; a draft until issued."""
    from apps.findings import selectors as finding_selectors

    visible = finding_selectors.get_finding(actor, finding.pk)
    if visible is None:
        raise ActionError(_("Unknown finding."), "unknown_finding")
    if not policies._at(actor, P.CORRECTIVE_ACTION_CREATE, visible.location):
        raise PermissionDenied
    if not policies.can_create_for(actor, visible):
        raise ActionConflict(_("A %(status)s finding cannot take new corrective actions.") % {"status": visible.get_status_display().lower()}, "finding_not_open")
    values = {k: _text(v, k.replace("_", " ").capitalize(), LIMITS[k], required=k == "title") for k, v in (
        ("title", title), ("description", description), ("plan", plan), ("expected_outcome", expected_outcome), ("completion_criteria", completion_criteria))}
    owner = _assignee(assigned_to, visible.location)
    lock_organization(visible.organization_id)
    action = CorrectiveAction(
        organization=visible.organization, number=next_number(visible.organization_id), finding=visible, location=visible.location, priority=_priority(priority),
        assigned_to=owner, due_at=_due(due_at), required_evidence=_required_evidence(required_evidence),
        independent_verification=bool(getattr(settings, "CORRECTIVE_ACTION_INDEPENDENT_VERIFICATION", True)), created_by=actor, **values,
    )
    stamp_history(action, actor, VERBS["created"])
    try:
        with transaction.atomic():
            action.save()
    except IntegrityError as exc:
        raise integrity_error(exc) from exc
    log(VERBS["created"], action, actor, f"Created {action.reference}: {action.title}", severity=visible.severity.code, priority=action.priority,
        assigned_to=str(owner.pk) if owner else None, finding_reference=visible.reference)
    events.send_on_commit(events.action_created, action, actor)
    if issue:
        action = _transition(action, "issue", actor, "")
    return action


# ---------------------------------------------------------------------------
@transaction.atomic
def update_action(action: CorrectiveAction, *, actor: Any, **fields: Any) -> CorrectiveAction:
    """Edit the plan.  The responsible user may change the plan text; a manager may also change title, due date, priority and the evidence
    requirement.  Status, owner, source and verification data are not reachable from here."""
    locked = lock_action(action.pk)
    unknown = set(fields) - set(WORKER_FIELDS) - set(MANAGER_FIELDS)
    if unknown:
        raise ActionError(_("These fields cannot be edited: %(f)s.") % {"f": ", ".join(sorted(unknown))}, "protected_field")
    if not policies.can_view(actor, locked):
        raise PermissionDenied
    wants_manager = any(k in fields for k in MANAGER_FIELDS)
    allowed = policies.can_manage(actor, locked) if wants_manager else policies.can_edit(actor, locked)
    if not allowed:
        if locked.status not in PLANNING_STATUSES and (policies.is_worker(actor, locked) or policies._at(actor, P.CORRECTIVE_ACTION_UPDATE, locked.location)):
            raise ActionConflict(_("A %(status)s action can no longer be edited.") % {"status": locked.get_status_display().lower()}, "not_editable")
        raise PermissionDenied
    data: dict[str, Any] = {}
    for key in ("title", "description", "plan", "expected_outcome", "completion_criteria"):
        if key in fields:
            data[key] = _text(fields[key], key.replace("_", " ").capitalize(), LIMITS[key], required=key == "title")
    if "priority" in fields:
        data["priority"] = _priority(fields["priority"])
    if "due_at" in fields:
        due = fields["due_at"]
        data["due_at"] = due if due == locked.due_at else _due(due)
    if "required_evidence" in fields:
        data["required_evidence"] = _required_evidence(fields["required_evidence"])
    changes = apply_changes(locked, data)
    if not changes:
        return locked
    stamp_history(locked, actor, VERBS["updated"])
    locked.save()
    log(VERBS["updated"], locked, actor, f"Updated {locked.reference}", changes=changes)
    if "due_at" in changes:
        log(VERBS["due_changed"], locked, actor, f"Due date of {locked.reference} changed", changes=changes["due_at"])
    if "priority" in changes:
        log(VERBS["priority_changed"], locked, actor, f"Priority of {locked.reference} changed to {locked.get_priority_display()}", changes=changes["priority"])
    events.send_on_commit(events.action_updated, locked, actor, changes=changes)
    return locked


@transaction.atomic
def assign(action: CorrectiveAction, *, actor: Any, assigned_to: Any) -> CorrectiveAction:
    """Set, change or (while still a draft or open) clear the responsible user.  First assignment needs ``assign``; a change needs ``reassign``."""
    locked = lock_action(action.pk)
    if not policies.can_view(actor, locked):
        raise PermissionDenied
    if not policies.can_assign(actor, locked):
        if locked.status not in PLANNING_STATUSES and policies._at(actor, policies.assign_permission(locked), locked.location):
            raise ActionConflict(_("A %(status)s action can no longer be reassigned.") % {"status": locked.get_status_display().lower()}, "not_editable")
        raise PermissionDenied
    owner = _assignee(assigned_to, locked.location)
    if owner is None and locked.status not in (ActionStatus.DRAFT, ActionStatus.OPEN):
        raise ActionError(_("Work has started: choose a new responsible user instead of clearing it."), "owner_required")
    changes = apply_changes(locked, {"assigned_to": owner})
    if not changes:
        return locked
    stamp_history(locked, actor, VERBS["assigned"])
    locked.save()
    log(VERBS["assigned"], locked, actor, f"{locked.reference} assigned to {owner.get_short_name() if owner else 'no one'}", changes=changes)
    events.send_on_commit(events.action_assigned, locked, actor, changes=changes)
    return locked


# ---------------------------------------------------------------------------
@transaction.atomic
def add_update(action: CorrectiveAction, *, actor: Any, body: str, progress_percent: Any = None, kind: str = UpdateKind.PROGRESS, corrects: Any = None) -> CorrectiveActionUpdate:
    """Append a progress note (or a correction of an earlier entry).  History is never rewritten; the status never changes."""
    locked = lock_action(action.pk)
    if not policies.can_view(actor, locked):
        raise PermissionDenied
    if not policies.can_add_update(actor, locked):
        if locked.status not in (ActionStatus.IN_PROGRESS, ActionStatus.BLOCKED) and policies.is_worker(actor, locked):
            raise ActionConflict(_("Progress can only be recorded while work is in progress or blocked."), "not_editable")
        raise PermissionDenied
    if kind not in (UpdateKind.PROGRESS, UpdateKind.CORRECTION):
        raise ActionError(_("Only progress notes and corrections are added here."), "invalid_kind")
    body = _text(body, _("The note"), MAX_UPDATE, required=True)
    if progress_percent is not None and (isinstance(progress_percent, bool) or not isinstance(progress_percent, int) or not 0 <= progress_percent <= 100):
        raise ActionError(_("Progress must be a whole number from 0 to 100."), "invalid_progress")
    target = None
    if kind == UpdateKind.CORRECTION:
        target = CorrectiveActionUpdate.objects.filter(pk=getattr(corrects, "pk", corrects), action=locked).first()
        if target is None:
            raise ActionError(_("Choose an entry of this action to correct."), "invalid_correction")
    update = CorrectiveActionUpdate(action=locked, kind=kind, author=actor, body=body, progress_percent=progress_percent, corrects=target)
    update.save()
    locked.save(update_fields=["updated_at"])
    log(VERBS["progress"], locked, actor, f"{'Correction' if target else 'Progress'} recorded on {locked.reference}", kind=kind, progress_percent=progress_percent,
        update_id=str(update.pk))
    events.send_on_commit(events.action_progress_added, locked, actor, update=update)
    return update


# ---------------------------------------------------------------------------
def transition(action: CorrectiveAction, name: str, *, actor: Any, reason: str = "") -> CorrectiveAction:
    """Run one lifecycle action.  ``reason`` is the blocker text (block), completion summary (submit), verification notes (verify) or the reason."""
    try:
        return _transition(action, name, actor, reason)
    except ActionError as exc:
        fresh = CorrectiveAction.objects.select_related("organization").get(pk=action.pk)
        log(VERBS["refused"], fresh, actor, f"{name} on {fresh.reference} refused", transition=name, code=exc.action_code)
        raise


@transaction.atomic
def _transition(action: CorrectiveAction, name: str, actor: Any, reason: str) -> CorrectiveAction:
    spec = TRANSITIONS.get(name)
    if spec is None:
        raise ActionError(_("Unknown action."), "unknown_action")
    locked = lock_action(action.pk)
    if not policies.can_view(actor, locked) or not policies.can_act(actor, locked, name):
        raise PermissionDenied
    for guard in hooks.transition_guards:  # e.g. a workflow governs this decision: it may only be taken through the workflow
        message = guard(locked, name, actor)
        if message:
            raise ActionError(message, "governed_by_workflow")
    if locked.status == spec.target and name != "reopen":
        raise ActionConflict(_("%(ref)s is already %(status)s; nothing was changed.") % {"ref": locked.reference, "status": locked.get_status_display().lower()}, "already_" + spec.target)
    if locked.status not in spec.sources:
        raise ActionError(_("%(action)s is not available for a %(status)s action.") % {"action": spec.label, "status": locked.get_status_display().lower()}, "invalid_transition")
    reason = (reason or "").strip()
    limit = {"block": MAX_BLOCKER, "submit": MAX_SUMMARY, "verify": MAX_NOTES, "reject": MAX_BLOCKER}.get(name, MAX_REASON)
    if spec.reason_required and not reason:
        message = {"block": _("Describe the blocker."), "submit": _("A completion summary is required.")}.get(name, _("A reason is required."))
        raise ActionError(message, "reason_required")
    if len(reason) > limit:
        raise ActionError(_("The text is too long."), "too_long")
    previous, now = locked.status, timezone.now()
    extra: dict[str, Any] = {}

    if name in ("issue", "start", "resume", "submit") and locked.assigned_to_id is None:
        raise ActionError(_("Assign a responsible user first."), "owner_required")
    if name == "issue":
        locked.issued_by, locked.issued_at = actor, now
    elif name == "start":
        locked.started_at = locked.started_at or now
    elif name == "block":
        locked.blocker, locked.blocked_at = reason, now
        _append(locked, UpdateKind.BLOCKER, actor, reason)
    elif name == "resume":
        if previous == ActionStatus.BLOCKED:
            _append(locked, UpdateKind.BLOCKER_CLEARED, actor, reason or f"Cleared: {locked.blocker}"[:MAX_UPDATE])
            locked.blocker, locked.blocked_at = "", None
    elif name == "submit":
        have, need = selectors.verified_completion_count(locked), locked.required_evidence
        if have < need:
            raise ActionError(_("%(need)d verified completion file(s) are required before submitting; %(have)d available. Files still being checked or quarantined do not count.")
                              % {"need": need, "have": have}, "evidence_required")
        locked.submission_count += 1
        locked.completion_summary, locked.submitted_by, locked.submitted_at = reason, actor, now
        if previous == ActionStatus.BLOCKED:  # submitting while blocked clears the blocker (documented policy)
            locked.blocker, locked.blocked_at = "", None
        _append(locked, UpdateKind.SUBMISSION, actor, reason, submission_number=locked.submission_count)
        extra["submission_number"] = locked.submission_count
    elif name in ("verify", "reject"):
        decision = Decision.APPROVED if name == "verify" else Decision.REJECTED
        try:
            with transaction.atomic():
                CorrectiveActionVerification.objects.create(
                    action=locked, submission_number=locked.submission_count, decision=decision, verifier=actor, decided_at=now,
                    notes=reason if name == "verify" else "", reason=reason if name == "reject" else "")
        except IntegrityError as exc:
            raise integrity_error(exc) from exc
        extra.update(decision=decision, submission_number=locked.submission_count)
        if name == "verify":
            locked.verified_by, locked.verified_at = actor, now
    elif name == "close":
        locked.closed_by, locked.closed_at = actor, now
    elif name == "cancel":
        locked.cancelled_by, locked.cancelled_at, locked.cancel_reason = actor, now, reason
    elif name == "reopen":
        locked.reopen_count += 1
        locked.verified_by = locked.verified_at = locked.closed_by = locked.closed_at = None
    locked.status = spec.target
    stamp_history(locked, actor, VERBS[name])
    try:
        with transaction.atomic():
            locked.save()
    except IntegrityError as exc:
        raise integrity_error(exc) from exc
    log(VERBS[name], locked, actor, f"{spec.label}: {locked.reference}", reason=reason or None, previous_status=previous, status=locked.status, **extra)
    events.send_on_commit(events.BY_TRANSITION[name], locked, actor, previous_status=previous)
    return locked


def _append(action: CorrectiveAction, kind: str, actor: Any, body: str, **extra: Any) -> CorrectiveActionUpdate:
    return CorrectiveActionUpdate.objects.create(action=action, kind=kind, author=actor, body=(body or "")[:MAX_UPDATE] or kind, **extra)
