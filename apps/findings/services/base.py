"""Shared plumbing of the finding services: audit verbs, locking, numbering, database-error mapping."""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError
from django.utils.translation import gettext_lazy as _

from apps.assignments.services.assignment import lock_organization  # noqa: F401  (re-exported: one numbering lock per organization)
from apps.core.services import log_activity

from ..models import Finding
from .errors import FindingConflict, FindingError

VERBS = {
    "created": "finding.created",
    "raised": "finding.raised",
    "detection_run": "finding.detection_run",
    "detection_failed": "finding.detection_failed",
    "updated": "finding.updated",
    "severity_changed": "finding.severity_changed",
    "category_changed": "finding.category_changed",
    "due_changed": "finding.due_date_changed",
    "assigned": "finding.assigned",
    "acknowledge": "finding.acknowledged",
    "start": "finding.started",
    "resolve": "finding.resolved",
    "close": "finding.closed",
    "reopen": "finding.reopened",
    "cancel": "finding.cancelled",
    "refused": "finding.transition_refused",
    "category_saved": "finding_category.saved",
    "severity_saved": "finding_severity.saved",
}


def log(verb: str, finding: Finding, actor: Any, description: str, **metadata: Any) -> None:
    """One audit entry; carries ``finding_id`` so the finding's timeline finds it.  No evidence content is ever logged."""
    log_activity(
        verb=verb, organization=finding.organization, actor=actor, target=finding, description=description,
        metadata={"finding_id": str(finding.pk), "reference": finding.reference, **metadata},
    )


def next_number(organization_id: Any) -> int:
    top = Finding.objects.filter(organization_id=organization_id).order_by("-number").values_list("number", flat=True).first()
    return (top or 0) + 1


def lock_finding(finding_id: Any) -> Finding:
    """The finding row locked ``FOR UPDATE`` (serialises lifecycle actions and edits of one finding)."""
    return (Finding.objects.select_for_update(of=("self",))
            .select_related("organization", "category", "severity", "location", "assigned_to", "execution").get(pk=finding_id))


def integrity_error(exc: IntegrityError) -> FindingError:
    """Map a database refusal to the rule it enforces (the services check first; this covers races)."""
    name = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", "") or ""
    if name == "one_finding_per_detection_key":
        return FindingConflict(_("This finding was already raised for the same trigger."), "duplicate")
    if name.startswith(("finding_", "unique_finding")):
        return FindingError(_("The finding breaks an integrity rule (%(rule)s).") % {"rule": name}, name)
    return FindingError(_("The finding conflicts with the current data. Reload and try again."), "conflict")
