"""Shared plumbing of the corrective-action services: audit verbs, locking, numbering, database-error mapping."""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError
from django.utils.translation import gettext_lazy as _

from apps.assignments.services.assignment import lock_organization  # noqa: F401  (re-exported: one numbering lock per organization)
from apps.core.services import log_activity

from ..models import CorrectiveAction
from .errors import ActionConflict, ActionError

VERBS = {
    "created": "corrective_action.created",
    "updated": "corrective_action.updated",
    "due_changed": "corrective_action.due_date_changed",
    "priority_changed": "corrective_action.priority_changed",
    "assigned": "corrective_action.assigned",
    "progress": "corrective_action.progress_added",
    "issue": "corrective_action.issued",
    "start": "corrective_action.started",
    "block": "corrective_action.blocked",
    "resume": "corrective_action.resumed",
    "submit": "corrective_action.submitted",
    "verify": "corrective_action.verified",
    "reject": "corrective_action.rejected",
    "close": "corrective_action.closed",
    "cancel": "corrective_action.cancelled",
    "reopen": "corrective_action.reopened",
    "refused": "corrective_action.transition_refused",
}


def log(verb: str, action: CorrectiveAction, actor: Any, description: str, **metadata: Any) -> None:
    """One audit entry; carries ``action_id`` and ``finding_id`` so both timelines find it.  No evidence content is ever logged."""
    log_activity(
        verb=verb, organization=action.organization, actor=actor, target=action, description=description,
        metadata={"action_id": str(action.pk), "finding_id": str(action.finding_id), "reference": action.reference, **metadata},
    )


def next_number(organization_id: Any) -> int:
    top = CorrectiveAction.objects.filter(organization_id=organization_id).order_by("-number").values_list("number", flat=True).first()
    return (top or 0) + 1


def lock_action(action_id: Any) -> CorrectiveAction:
    """The action row locked ``FOR UPDATE`` (serialises lifecycle actions, edits, updates, evidence and decisions on one action)."""
    return (CorrectiveAction.objects.select_for_update(of=("self",))
            .select_related("organization", "finding", "location", "assigned_to", "created_by", "submitted_by").get(pk=action_id))


def integrity_error(exc: IntegrityError) -> ActionError:
    """Map a database refusal to the rule it enforces (the services check first; this covers races)."""
    name = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", "") or ""
    if name == "one_verification_per_submission":
        return ActionConflict(_("This submission was already decided."), "already_decided")
    if name.startswith(("action_", "unique_action")):
        return ActionError(_("The action breaks an integrity rule (%(rule)s).") % {"rule": name}, name)
    return ActionError(_("The action conflicts with the current data. Reload and try again."), "conflict")
