"""Vocabulary of checklist assignments (ADR 0051).

Persisted statuses: ``draft``, ``assigned``, ``cancelled`` (set by Phase 8) and ``in_progress``, ``completed`` (reserved:
only Phase 9's execution integration may set them - no Phase 8 code path does).  ``overdue`` is *derived*, never stored:
an open assignment whose due date has passed (see ``selectors`` and ``Assignment.effective_status``).
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class AssignmentStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    ASSIGNED = "assigned", _("Assigned")
    IN_PROGRESS = "in_progress", _("In progress")
    COMPLETED = "completed", _("Completed")
    CANCELLED = "cancelled", _("Cancelled")


#: The derived presentation status of an open assignment past its due date.
OVERDUE = "overdue"

#: Statuses in which an assignment is *open* work (counts for duplicate detection and can become overdue).
OPEN_STATUSES = (AssignmentStatus.ASSIGNED, AssignmentStatus.IN_PROGRESS)


class AssignmentPriority(models.TextChoices):
    LOW = "low", _("Low")
    NORMAL = "normal", _("Normal")
    HIGH = "high", _("High")
    CRITICAL = "critical", _("Critical")


#: Sort weight of each priority (higher = more urgent).
PRIORITY_RANK = {"low": 1, "normal": 2, "high": 3, "critical": 4}

#: Persisted transitions Phase 8 may perform (``in_progress`` / ``completed`` belong to Phase 9).
TRANSITIONS: dict[str, frozenset[str]] = {
    AssignmentStatus.DRAFT: frozenset({AssignmentStatus.ASSIGNED, AssignmentStatus.CANCELLED}),
    AssignmentStatus.ASSIGNED: frozenset({AssignmentStatus.CANCELLED}),
    AssignmentStatus.IN_PROGRESS: frozenset({AssignmentStatus.CANCELLED}),
    AssignmentStatus.COMPLETED: frozenset(),
    AssignmentStatus.CANCELLED: frozenset({AssignmentStatus.DRAFT}),  # an explicit, authorized reopen
}

DEFAULT_DUE_SOON_HOURS = 48
