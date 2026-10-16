"""The checklist assignment (Phase 8): ``Organization + exact published ChecklistVersion + generic Location``.

An assignment references the *version* (never the template) so a later publication cannot change what was assigned, and
the location by its generic ``Location`` row - there is nothing restaurant-specific.  Phase 9 executions will reference the
assignment and read organization, location and version through it instead of copying them.

Integrity: all three foreign keys are ``PROTECT`` (history is never cascaded away); the organization of the version's
template and of the location must equal the assignment's (enforced by a PostgreSQL trigger, ``guards.py``, and by the
services); status, lifecycle columns and the "one open assignment per version and location" policy are database constraints.
Status is changed only by ``services.lifecycle`` - never by a form, serializer, import or the admin.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone
from simple_history.models import HistoricalRecords

from apps.core.models import BaseModel

from .constants import OPEN_STATUSES, OVERDUE, AssignmentPriority, AssignmentStatus

User = settings.AUTH_USER_MODEL


class ChecklistAssignment(BaseModel):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, related_name="checklist_assignments")
    #: Sequential per organization (``ASG-00042``), allocated under the organization row lock.
    number = models.PositiveIntegerField()
    version = models.ForeignKey("checklists.ChecklistVersion", on_delete=models.PROTECT, related_name="assignments")
    location = models.ForeignKey("locations.Location", on_delete=models.PROTECT, related_name="checklist_assignments")
    status = models.CharField(max_length=12, choices=AssignmentStatus.choices, default=AssignmentStatus.DRAFT, db_index=True)
    priority = models.CharField(max_length=10, choices=AssignmentPriority.choices, default=AssignmentPriority.NORMAL)
    instructions = models.TextField(blank=True, max_length=2000)
    #: The designated responsible user (optional; an assignment is otherwise for the location's eligible users).
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="checklist_assignments")
    due_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    issued_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    issued_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=300, blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="unique_assignment_number_per_organization"),
            # Duplicate policy (ADR 0051): one *open* assignment per exact version and location.
            models.UniqueConstraint(
                fields=["version", "location"], condition=models.Q(status__in=["assigned", "in_progress"]),
                name="one_open_assignment_per_version_location",
            ),
            models.CheckConstraint(condition=models.Q(status__in=[s for s, _l in AssignmentStatus.choices]), name="assignment_status_valid"),
            models.CheckConstraint(condition=models.Q(priority__in=[p for p, _l in AssignmentPriority.choices]), name="assignment_priority_valid"),
            models.CheckConstraint(
                condition=models.Q(status="draft", issued_at__isnull=True, cancelled_at__isnull=True)
                | models.Q(status__in=["assigned", "in_progress", "completed"], issued_at__isnull=False, cancelled_at__isnull=True)
                | models.Q(status="cancelled", cancelled_at__isnull=False) & ~models.Q(cancel_reason=""),
                name="assignment_lifecycle_consistent",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status", "due_at"]),
            models.Index(fields=["organization", "location", "status"]),
            models.Index(fields=["version", "status"]),
            models.Index(fields=["assigned_to", "status"]),
            models.Index(fields=["organization", "priority"]),
        ]

    def __str__(self) -> str:
        return self.reference

    @property
    def reference(self) -> str:
        return f"ASG-{self.number:05d}"

    def get_absolute_url(self) -> str:
        from django.urls import reverse

        return reverse("assignments:detail", args=[self.pk])

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def is_overdue(self) -> bool:
        """Derived, never stored: open work whose due date has passed (recomputed on every read)."""
        return bool(self.is_open and self.due_at and self.due_at < timezone.now())

    @property
    def effective_status(self) -> str:
        return OVERDUE if self.is_overdue else self.status
