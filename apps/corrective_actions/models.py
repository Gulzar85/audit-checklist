"""Corrective action domain models (Phase 13).

* ``CorrectiveAction``: an independently tracked unit of work against one ``Finding``.  Many actions per finding (relational, never a
  JSON array).  Organization and location are the finding's (a trigger keeps them equal).  Status changes only through
  ``services.lifecycle``.  Overdue is derived from ``due_at`` and the status, never stored.
* ``CorrectiveActionUpdate``: an append-only entry (progress, blocker, submission, correction).  A correction is a *new* entry that
  points at the one it corrects; nothing is ever rewritten or deleted (trigger).
* ``CorrectiveActionVerification``: an append-only decision (approved / rejected) by someone other than the owner, one per submission.
Evidence is attached through the Phase 11 registry (``EvidenceAttachment.corrective_action`` + ``purpose``), never copied.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone
from simple_history.models import HistoricalRecords

from apps.core.models import BaseModel

from .constants import OVERDUE, PRIORITY_RANK, WORKING_STATUSES, ActionStatus, Decision, Priority, UpdateKind

User = settings.AUTH_USER_MODEL


class CorrectiveAction(BaseModel):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, related_name="corrective_actions")
    #: Sequential per organization (``CA-00042``), allocated under the organization row lock.
    number = models.PositiveIntegerField()
    finding = models.ForeignKey("findings.Finding", on_delete=models.PROTECT, related_name="corrective_actions")
    #: The finding's location, copied once (trigger-checked, immutable): scope checks and indexes need no join.
    location = models.ForeignKey("locations.Location", on_delete=models.PROTECT, related_name="corrective_actions")
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, max_length=2000)
    # -- the structured plan (all optional) -----------------------------------------------------------------------------------
    plan = models.TextField(blank=True, max_length=2000, help_text="Planned activities.")
    expected_outcome = models.CharField(max_length=500, blank=True)
    completion_criteria = models.CharField(max_length=500, blank=True)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.NORMAL)
    status = models.CharField(max_length=12, choices=ActionStatus.choices, default=ActionStatus.DRAFT, db_index=True)
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="corrective_actions")
    #: A timestamp (the platform stores instants; forms take a local date-time).  Overdue = past this while the owner still has work.
    due_at = models.DateTimeField(null=True, blank=True)
    #: Policy snapshots taken at creation, so later settings changes never alter an action already under way.
    required_evidence = models.PositiveSmallIntegerField(default=0, help_text="Verified completion files needed before the action can be submitted.")
    independent_verification = models.BooleanField(default=True, help_text="The verifier must be neither the owner nor the submitter.")

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    issued_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    issued_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    blocker = models.CharField(max_length=500, blank=True)
    blocked_at = models.DateTimeField(null=True, blank=True)
    # -- submission / verification / closure (the latest cycle; every decision also lives in CorrectiveActionVerification) -----------
    submission_count = models.PositiveSmallIntegerField(default=0)
    completion_summary = models.TextField(blank=True, max_length=1000)
    submitted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    submitted_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    verified_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    closed_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=300, blank=True)
    reopen_count = models.PositiveSmallIntegerField(default=0)

    history = HistoricalRecords()

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="unique_action_number_per_organization"),
            models.CheckConstraint(condition=models.Q(status__in=[s for s, _l in ActionStatus.choices]), name="action_status_valid"),
            models.CheckConstraint(condition=models.Q(priority__in=[p for p, _l in Priority.choices]), name="action_priority_valid"),
            models.CheckConstraint(condition=~models.Q(title=""), name="action_title_present"),
            models.CheckConstraint(condition=models.Q(required_evidence__lte=10), name="action_required_evidence_valid"),
            models.CheckConstraint(condition=models.Q(status="draft") | models.Q(issued_at__isnull=False), name="action_issued_has_timestamp"),
            models.CheckConstraint(condition=~models.Q(status="blocked") | ~models.Q(blocker=""), name="action_blocked_has_blocker"),
            models.CheckConstraint(
                condition=~models.Q(status="submitted") | (models.Q(submitted_at__isnull=False) & models.Q(submission_count__gte=1) & ~models.Q(completion_summary="")),
                name="action_submitted_has_submission",
            ),
            models.CheckConstraint(condition=~models.Q(status__in=["verified", "closed"]) | models.Q(verified_at__isnull=False), name="action_verified_has_timestamp"),
            models.CheckConstraint(condition=~models.Q(status="closed") | models.Q(closed_at__isnull=False), name="action_closed_has_timestamp"),
            models.CheckConstraint(
                condition=~models.Q(status="cancelled") | (models.Q(cancelled_at__isnull=False) & ~models.Q(cancel_reason="")), name="action_cancelled_has_reason",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status", "due_at"]),
            models.Index(fields=["organization", "location", "status"]),
            models.Index(fields=["finding", "status"]),
            models.Index(fields=["assigned_to", "status"]),
            models.Index(fields=["organization", "priority"]),
        ]

    def __str__(self) -> str:
        return f"{self.reference} {self.title}"

    @property
    def reference(self) -> str:
        return f"CA-{self.number:05d}"

    def get_absolute_url(self) -> str:
        from django.urls import reverse

        return reverse("corrective_actions:detail", args=[self.pk])

    @property
    def priority_rank(self) -> int:
        return PRIORITY_RANK.get(self.priority, 0)

    @property
    def is_overdue(self) -> bool:
        """Derived on every read, never stored (ADR 0066): a due date in the past while the owner still has work to do."""
        return bool(self.status in WORKING_STATUSES and self.due_at and self.due_at < timezone.now())

    @property
    def effective_status(self) -> str:
        return OVERDUE if self.is_overdue else self.status


class CorrectiveActionUpdate(BaseModel):
    """An append-only entry in the action's work log."""

    action = models.ForeignKey(CorrectiveAction, on_delete=models.PROTECT, related_name="updates")
    kind = models.CharField(max_length=16, choices=UpdateKind.choices, default=UpdateKind.PROGRESS)
    author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    body = models.TextField(max_length=2000)
    #: Optional 0-100 self-assessment.  It is *information*: it never changes the status, and 100 is not verification.
    progress_percent = models.PositiveSmallIntegerField(null=True, blank=True)
    #: For ``submission`` entries: which submission cycle this is (1, 2 ...), matching ``CorrectiveActionVerification.submission_number``.
    submission_number = models.PositiveSmallIntegerField(null=True, blank=True)
    corrects = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="corrections")

    class Meta:
        ordering = ("created_at",)
        constraints = [
            models.CheckConstraint(condition=models.Q(kind__in=[k for k, _l in UpdateKind.choices]), name="action_update_kind_valid"),
            models.CheckConstraint(condition=models.Q(progress_percent__isnull=True) | models.Q(progress_percent__lte=100), name="action_update_percent_valid"),
            models.CheckConstraint(condition=~models.Q(body=""), name="action_update_body_present"),
            models.CheckConstraint(
                condition=~models.Q(kind="submission") | models.Q(submission_number__isnull=False), name="action_update_submission_numbered",
            ),
            models.CheckConstraint(condition=~models.Q(kind="correction") | models.Q(corrects__isnull=False), name="action_update_correction_targets"),
        ]
        indexes = [models.Index(fields=["action", "created_at"])]

    def __str__(self) -> str:
        return f"{self.get_kind_display()}: {self.body[:40]}"


class CorrectiveActionVerification(BaseModel):
    """An append-only verification decision, one per submission cycle.  Rejection needs a reason; the verifier is never client-supplied."""

    action = models.ForeignKey(CorrectiveAction, on_delete=models.PROTECT, related_name="verifications")
    submission_number = models.PositiveSmallIntegerField()
    decision = models.CharField(max_length=10, choices=Decision.choices)
    verifier = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    notes = models.TextField(blank=True, max_length=1000)
    reason = models.CharField(max_length=500, blank=True)
    decided_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("decided_at",)
        constraints = [
            models.UniqueConstraint(fields=["action", "submission_number"], name="one_verification_per_submission"),
            models.CheckConstraint(condition=models.Q(decision__in=[d for d, _l in Decision.choices]), name="action_verification_decision_valid"),
            models.CheckConstraint(condition=~models.Q(decision="rejected") | ~models.Q(reason=""), name="action_rejection_has_reason"),
        ]
        indexes = [models.Index(fields=["action", "decided_at"])]

    def __str__(self) -> str:
        return f"{self.get_decision_display()} (submission {self.submission_number})"
