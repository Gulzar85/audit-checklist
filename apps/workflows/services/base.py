"""Shared plumbing of the workflow services: audit verbs, locking, numbering, event recording, database-error mapping."""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError
from django.db.models import Max
from django.utils.translation import gettext_lazy as _

from apps.assignments.services.assignment import lock_organization  # noqa: F401  (re-exported: one numbering lock per organization)
from apps.core.services import log_activity

from ..models import WorkflowEvent, WorkflowInstance
from .errors import WorkflowConflict, WorkflowError

VERBS = {
    "definition_created": "workflow.definition_created",
    "definition_updated": "workflow.definition_updated",
    "definition_archived": "workflow.definition_archived",
    "version_created": "workflow.version_created",
    "version_updated": "workflow.version_updated",
    "version_validated": "workflow.version_validated",
    "version_published": "workflow.version_published",
    "version_archived": "workflow.version_archived",
    "config_changed": "workflow.config_changed",
    "started": "workflow.started",
    "state_entered": "workflow.state_entered",
    "transition": "workflow.transition_completed",
    "transition_refused": "workflow.transition_refused",
    "approval": "workflow.approval_recorded",
    "task_created": "workflow.task_created",
    "task_claimed": "workflow.task_claimed",
    "task_released": "workflow.task_released",
    "task_reassigned": "workflow.task_reassigned",
    "handler_invoked": "workflow.handler_invoked",
    "handler_failed": "workflow.handler_failed",
    "completed": "workflow.completed",
    "cancelled": "workflow.cancelled",
    "attention": "workflow.needs_attention",
}


def log(verb: str, instance: WorkflowInstance, actor: Any, description: str, **metadata: Any) -> None:
    """One audit entry carrying the instance, the exact version and the target - never a comment longer than the transition's own."""
    log_activity(
        verb=verb, organization=instance.organization, actor=actor, target=instance, description=description,
        metadata={"workflow_instance_id": str(instance.pk), "reference": instance.reference, "workflow": instance.definition.code, "version": instance.version.number,
                  "target_type": instance.target_type, "target_id": str(instance.target_id), **metadata},
    )


def lock_instance(instance_id: Any) -> WorkflowInstance:
    """The instance row locked ``FOR UPDATE``: serialises transitions, approvals, claims and cancellation of one workflow.

    The lock query has **no joins**: PostgreSQL re-checks a locked row's join conditions against the *old* partner rows when the row was changed
    concurrently (``state_id`` changes on every transition), which would silently drop it.  The related rows are loaded by a second query once the lock is held.
    """
    WorkflowInstance.objects.select_for_update(of=("self",)).get(pk=instance_id)
    return WorkflowInstance.objects.select_related("organization", "definition", "version", "state", "location", "started_by").get(pk=instance_id)


def record_event(instance: WorkflowInstance, kind: str, actor: Any = None, **fields: Any) -> WorkflowEvent:
    """Append an event with the next sequence number (the instance row is locked by the caller)."""
    top = WorkflowEvent.objects.filter(instance=instance).aggregate(m=Max("seq"))["m"] or 0
    return WorkflowEvent.objects.create(instance=instance, seq=top + 1, kind=kind, actor=actor if getattr(actor, "is_authenticated", False) else None,
                                        entry_seq=fields.pop("entry_seq", instance.entry_seq), **fields)


def next_number(organization_id: Any) -> int:
    top = WorkflowInstance.objects.filter(organization_id=organization_id).aggregate(m=Max("number"))["m"]
    return (top or 0) + 1


def integrity_error(exc: IntegrityError) -> WorkflowError:
    """Map a database refusal to the rule it enforces (the services check first; this covers races)."""
    name = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", "") or ""
    if name == "one_active_instance_per_target_definition":
        return WorkflowConflict(_("This record already has an active workflow of this kind."), "already_running")
    if name == "one_event_per_idempotency_key":
        return WorkflowConflict(_("This request was already processed."), "duplicate_request")
    if name == "one_approval_per_actor_and_entry":
        return WorkflowConflict(_("You have already recorded your approval for this step."), "already_approved")
    if name in ("workflow_published_config_immutable", "workflow_version_immutable"):
        return WorkflowConflict(_("A published workflow version is immutable; create a new draft version."), "version_immutable")
    if name.startswith(("workflow_", "unique_workflow", "one_")):
        return WorkflowError(_("The change breaks an integrity rule (%(rule)s).") % {"rule": name}, name)
    return WorkflowError(_("The workflow conflicts with the current data. Reload and try again."), "conflict")
