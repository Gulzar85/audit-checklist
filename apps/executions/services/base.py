"""Shared plumbing of the execution services: audit verbs, locking, numbering, database-error mapping."""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError
from django.utils.translation import gettext_lazy as _

from apps.assignments.services.assignment import lock_organization  # noqa: F401  (re-exported: one numbering lock per organization)
from apps.core.services import log_activity

from ..models import ChecklistExecution
from .errors import ExecutionConflict, ExecutionError

VERBS = {
    "started": "execution.started",
    "saved": "execution.responses_saved",
    "submitted": "execution.submitted",
    "refused": "execution.submission_refused",
    "cancelled": "execution.cancelled",
}


def log(verb: str, execution: ChecklistExecution, actor: Any, description: str, **metadata: Any) -> None:
    """One audit entry.  ``assignment_id`` is included so the assignment's timeline shows its execution events too."""
    log_activity(
        verb=verb, organization=execution.organization, actor=actor, target=execution, description=description,
        metadata={"execution_id": str(execution.pk), "assignment_id": str(execution.assignment_id), "reference": execution.reference, **metadata},
    )


def next_number(organization_id: Any) -> int:
    top = ChecklistExecution.objects.filter(organization_id=organization_id).order_by("-number").values_list("number", flat=True).first()
    return (top or 0) + 1


def integrity_error(exc: IntegrityError) -> ExecutionError:
    """Map a database refusal to the rule it enforces (the services check first; this covers races)."""
    name = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", "") or ""
    if name == "one_live_execution_per_assignment":
        return ExecutionConflict(_("This assignment already has an execution; open it instead."), "duplicate")
    if name.startswith(("execution_", "response_")):
        return ExecutionError(_("The change breaks an integrity rule (%(rule)s).") % {"rule": name}, name)
    return ExecutionError(_("The execution conflicts with the current data. Reload and try again."), "conflict")


def lock_execution(execution_id: Any) -> ChecklistExecution:
    """The execution row locked ``FOR UPDATE`` (serialises saves, submission and cancellation of one execution)."""
    return (ChecklistExecution.objects.select_for_update(of=("self",))
            .select_related("assignment__location", "assignment__organization", "version__template", "organization").get(pk=execution_id))
