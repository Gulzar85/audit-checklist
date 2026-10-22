"""Vocabulary of checklist executions (ADR 0054).

Persisted statuses: ``in_progress`` (editable), ``submitted`` (final, read-only), ``cancelled`` (kept for history).  There is no
``draft`` (it would equal ``in_progress``), and no ``verified`` / ``closed`` - review belongs to later phases.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class ExecutionStatus(models.TextChoices):
    IN_PROGRESS = "in_progress", _("In progress")
    SUBMITTED = "submitted", _("Submitted")
    CANCELLED = "cancelled", _("Cancelled")


#: "Live" executions occupy the assignment's single execution slot; a cancelled one frees it.
LIVE_STATUSES = (ExecutionStatus.IN_PROGRESS, ExecutionStatus.SUBMITTED)

TRANSITIONS: dict[str, frozenset[str]] = {
    ExecutionStatus.IN_PROGRESS: frozenset({ExecutionStatus.SUBMITTED, ExecutionStatus.CANCELLED}),
    ExecutionStatus.SUBMITTED: frozenset(),  # final: no reopen in Phase 9
    ExecutionStatus.CANCELLED: frozenset(),
}


class ResponseStatus(models.TextChoices):
    ANSWERED = "answered", _("Answered")
    NOT_APPLICABLE = "not_applicable", _("Not applicable")
