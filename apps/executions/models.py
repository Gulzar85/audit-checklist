"""The runtime side of the platform: executions and their responses (Phase 9).

``Organization -> ChecklistAssignment -> ChecklistVersion -> ChecklistExecution -> ChecklistResponse``

An execution is one user's attempt at one issued assignment.  It *references* the immutable published version (it copies no
questions) and its responses are relational rows keyed by checklist item.  ``organization`` and ``version`` are copied from the
assignment only so that integrity can be checked and scoped queries stay cheap; a PostgreSQL trigger (``guards.py``) keeps them
equal to the assignment's and forbids ever changing them.  Status changes only through ``services`` (never a form, serializer,
import or the admin); responses of a submitted or cancelled execution are frozen by the same trigger.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone
from simple_history.models import HistoricalRecords

from apps.core.models import BaseModel

from .constants import ExecutionStatus, ResponseStatus

User = settings.AUTH_USER_MODEL


class ChecklistExecution(BaseModel):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, related_name="checklist_executions")
    #: Sequential per organization (``EXE-00042``), allocated under the organization row lock.
    number = models.PositiveIntegerField()
    assignment = models.ForeignKey("assignments.ChecklistAssignment", on_delete=models.PROTECT, related_name="executions")
    #: The assignment's exact published version, copied once at start (trigger-checked, immutable).
    version = models.ForeignKey("checklists.ChecklistVersion", on_delete=models.PROTECT, related_name="executions")
    status = models.CharField(max_length=12, choices=ExecutionStatus.choices, default=ExecutionStatus.IN_PROGRESS, db_index=True)
    started_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    started_at = models.DateTimeField(default=timezone.now)
    last_activity_at = models.DateTimeField(default=timezone.now)
    submitted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    submitted_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=300, blank=True)
    #: Optimistic-concurrency token of the whole execution: bumped by every accepted write.
    revision = models.PositiveIntegerField(default=1)

    history = HistoricalRecords()

    class Meta:
        ordering = ("-started_at",)
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="unique_execution_number_per_organization"),
            # One live execution per assignment; cancelling one frees the slot (ADR 0054).
            models.UniqueConstraint(
                fields=["assignment"], condition=models.Q(status__in=["in_progress", "submitted"]), name="one_live_execution_per_assignment",
            ),
            models.CheckConstraint(condition=models.Q(status__in=[s for s, _l in ExecutionStatus.choices]), name="execution_status_valid"),
            models.CheckConstraint(
                condition=models.Q(status="in_progress", submitted_at__isnull=True, cancelled_at__isnull=True)
                | models.Q(status="submitted", submitted_at__isnull=False, submitted_by__isnull=False, cancelled_at__isnull=True)
                | models.Q(status="cancelled", cancelled_at__isnull=False, submitted_at__isnull=True) & ~models.Q(cancel_reason=""),
                name="execution_lifecycle_consistent",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status", "started_at"]),
            models.Index(fields=["assignment", "status"]),
            models.Index(fields=["version"]),
            models.Index(fields=["started_by", "status"]),
        ]

    def __str__(self) -> str:
        return self.reference

    @property
    def reference(self) -> str:
        return f"EXE-{self.number:05d}"

    def get_absolute_url(self) -> str:
        from django.urls import reverse

        return reverse("executions:workspace", args=[self.pk])

    @property
    def is_editable(self) -> bool:
        return self.status == ExecutionStatus.IN_PROGRESS


class ChecklistResponse(BaseModel):
    """The current answer to one checklist item within one execution (one row per execution and item)."""

    execution = models.ForeignKey(ChecklistExecution, on_delete=models.CASCADE, related_name="responses")
    item = models.ForeignKey("checklists.ChecklistItem", on_delete=models.PROTECT, related_name="responses")
    status = models.CharField(max_length=14, choices=ResponseStatus.choices, default=ResponseStatus.ANSWERED)
    #: The *serialized* normalized value produced by the Phase 5 validator for the item's answer type; never raw client input.
    value = models.JSONField(null=True, blank=True)
    comment = models.TextField(blank=True, max_length=2000)
    #: Per-response optimistic-concurrency token (the client sends the revision it last saw).
    revision = models.PositiveIntegerField(default=1)
    #: Stamped at submission for answers whose item the rules had hidden: kept for audit, ignored by read-only views and progress.
    is_hidden = models.BooleanField(default=False)
    answered_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    answered_at = models.DateTimeField(default=timezone.now)

    history = HistoricalRecords()

    class Meta:
        ordering = ("item__section__sort_order", "item__sort_order")
        constraints = [
            models.UniqueConstraint(fields=["execution", "item"], name="one_response_per_execution_item"),
            models.CheckConstraint(condition=models.Q(status__in=[s for s, _l in ResponseStatus.choices]), name="response_status_valid"),
            models.CheckConstraint(
                condition=models.Q(status="answered", value__isnull=False) | models.Q(status="not_applicable"),
                name="response_value_matches_status",
            ),
        ]
        indexes = [models.Index(fields=["execution", "item"]), models.Index(fields=["item"])]

    def __str__(self) -> str:
        return f"{self.execution.reference}: {self.item_id}"
