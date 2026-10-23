"""Findings domain models (Phase 12).

* ``FindingCategory`` / ``FindingSeverity``: organization-owned configuration.  Severity is ordered by an integer ``rank`` - never by
  its label - and carries the policy hooks (owner required, default due days, escalation flag) later phases read.
* ``Finding``: the traceable business record.  A checklist-derived finding keeps **explicit foreign keys** to the organization,
  location, assignment, execution, the *exact* checklist version, the item and (when there is one) the response that triggered it,
  plus the rule that fired.  There is no ``GenericForeignKey``; a source is one of the registered ``SourceType`` values.
* Idempotency: ``detection_key`` is the deterministic identity of a rule-raised finding; a partial unique constraint makes a second
  creation for the same trigger impossible, whatever the concurrency.
* Status changes only through ``services.lifecycle``.  Findings are never deleted (a trigger enforces it); a cancelled finding keeps
  its history and is not recreated.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone
from simple_history.models import HistoricalRecords

from apps.core.models import BaseModel

from .constants import ACTIVE_STATUSES, OVERDUE, FindingStatus, SourceType

User = settings.AUTH_USER_MODEL


class FindingCategory(BaseModel):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, related_name="finding_categories")
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=300, blank=True)
    is_active = models.BooleanField(default=True)
    #: Seeded starting data: may be renamed or deactivated, but its code is permanent and it is never deleted.
    is_system = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)

    history = HistoricalRecords()

    class Meta:
        ordering = ("sort_order", "name")
        verbose_name_plural = "finding categories"
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="unique_finding_category_code"),
            models.CheckConstraint(condition=models.Q(code__regex=r"^[a-z0-9][a-z0-9_-]{0,39}$"), name="finding_category_code_valid"),
        ]
        indexes = [models.Index(fields=["organization", "is_active"])]

    def __str__(self) -> str:
        return self.name


class FindingSeverity(BaseModel):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, related_name="finding_severities")
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=100)
    #: 1-100, higher = more severe.  The only thing code compares.
    rank = models.PositiveSmallIntegerField()
    is_default = models.BooleanField(default=False, help_text="Used when a rule names a severity that does not exist (the finding is flagged for review).")
    is_active = models.BooleanField(default=True)
    requires_owner = models.BooleanField(default=False, help_text="Work cannot start until a responsible user is assigned.")
    default_due_days = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Due date set at detection; none is invented when blank.")
    is_escalation = models.BooleanField(default=False, help_text="Metadata for the future escalation / notification phase; nothing is sent in Phase 12.")
    is_system = models.BooleanField(default=False)

    history = HistoricalRecords()

    class Meta:
        ordering = ("-rank",)
        verbose_name_plural = "finding severities"
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="unique_finding_severity_code"),
            models.UniqueConstraint(fields=["organization", "rank"], name="unique_finding_severity_rank"),
            models.UniqueConstraint(fields=["organization"], condition=models.Q(is_default=True), name="one_default_finding_severity"),
            models.CheckConstraint(condition=models.Q(code__regex=r"^[a-z0-9][a-z0-9_-]{0,39}$"), name="finding_severity_code_valid"),
            models.CheckConstraint(condition=models.Q(rank__gte=1, rank__lte=100), name="finding_severity_rank_valid"),
            models.CheckConstraint(condition=models.Q(default_due_days__isnull=True) | models.Q(default_due_days__gte=1), name="finding_severity_due_days_valid"),
        ]
        indexes = [models.Index(fields=["organization", "is_active"])]

    def __str__(self) -> str:
        return self.name

    @property
    def tone(self) -> str:
        """The semantic badge tone of this level (never a colour): escalating levels are danger, then by rank."""
        if self.is_escalation:
            return "danger"
        return "warning" if self.rank >= 30 else "info" if self.rank >= 20 else "neutral"


class Finding(BaseModel):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, related_name="findings")
    #: Sequential per organization (``FND-00042``), allocated under the organization row lock.
    number = models.PositiveIntegerField()
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, max_length=2000)
    category = models.ForeignKey(FindingCategory, on_delete=models.PROTECT, related_name="findings")
    severity = models.ForeignKey(FindingSeverity, on_delete=models.PROTECT, related_name="findings")
    status = models.CharField(max_length=12, choices=FindingStatus.choices, default=FindingStatus.OPEN, db_index=True)
    source_type = models.CharField(max_length=16, choices=SourceType.choices)
    #: ``True`` when a rule named a category/severity that could not be resolved, or the rule asks for review: a person must look first.
    needs_review = models.BooleanField(default=False)
    #: Show the triggering response's evidence with the finding (read-through, never copied).
    inherit_evidence = models.BooleanField(default=True)
    location = models.ForeignKey("locations.Location", on_delete=models.PROTECT, related_name="findings")

    # -- source traceability (checklist-derived findings; all PROTECT, immutable) -------------------------------------------
    assignment = models.ForeignKey("assignments.ChecklistAssignment", on_delete=models.PROTECT, null=True, blank=True, related_name="findings")
    execution = models.ForeignKey("executions.ChecklistExecution", on_delete=models.PROTECT, null=True, blank=True, related_name="findings")
    #: The exact checklist version the execution ran; later publications never change what a finding refers to.
    version = models.ForeignKey("checklists.ChecklistVersion", on_delete=models.PROTECT, null=True, blank=True, related_name="findings")
    item = models.ForeignKey("checklists.ChecklistItem", on_delete=models.PROTECT, null=True, blank=True, related_name="findings")
    response = models.ForeignKey("executions.ChecklistResponse", on_delete=models.PROTECT, null=True, blank=True, related_name="findings")
    #: The detection rule (by its code inside the version) and a snapshot of its name - reproducible from the immutable version.
    rule_code = models.CharField(max_length=64, blank=True)
    rule_name = models.CharField(max_length=200, blank=True)
    #: Deterministic identity of a rule-raised finding: ``<execution>:<item>:<rule>#<action index>``.  Empty for manual findings.
    detection_key = models.CharField(max_length=200, blank=True)
    detected_at = models.DateTimeField(default=timezone.now)
    #: Who submitted the execution (rule) or created the finding (manual).  Never writable by a client.
    detected_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    # -- ownership and dates ----------------------------------------------------------------------------------------------
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_findings")
    due_at = models.DateTimeField(null=True, blank=True)

    # -- lifecycle --------------------------------------------------------------------------------------------------------
    resolution_summary = models.TextField(blank=True, max_length=1000)
    resolved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    closed_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=300, blank=True)
    reopen_count = models.PositiveSmallIntegerField(default=0)

    history = HistoricalRecords()

    class Meta:
        ordering = ("-detected_at",)
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="unique_finding_number_per_organization"),
            # Idempotent detection: one finding per trigger, whatever the concurrency (ADR 0064).
            models.UniqueConstraint(fields=["organization", "detection_key"], condition=~models.Q(detection_key=""), name="one_finding_per_detection_key"),
            models.CheckConstraint(condition=models.Q(status__in=[s for s, _l in FindingStatus.choices]), name="finding_status_valid"),
            models.CheckConstraint(condition=models.Q(source_type__in=[s for s, _l in SourceType.choices]), name="finding_source_valid"),
            models.CheckConstraint(condition=~models.Q(title=""), name="finding_title_present"),
            models.CheckConstraint(
                condition=~models.Q(source_type=SourceType.CHECKLIST_RULE)
                | (models.Q(assignment__isnull=False, execution__isnull=False, version__isnull=False, item__isnull=False) & ~models.Q(detection_key="") & ~models.Q(rule_code="")),
                name="finding_rule_source_complete",
            ),
            models.CheckConstraint(
                condition=models.Q(execution__isnull=True) | (models.Q(assignment__isnull=False, version__isnull=False)),
                name="finding_execution_context_complete",
            ),
            models.CheckConstraint(condition=models.Q(item__isnull=True) | models.Q(execution__isnull=False), name="finding_item_needs_execution"),
            models.CheckConstraint(
                condition=~models.Q(status__in=["resolved", "closed"]) | models.Q(resolved_at__isnull=False),
                name="finding_resolved_has_timestamp",
            ),
            models.CheckConstraint(condition=~models.Q(status="closed") | models.Q(closed_at__isnull=False), name="finding_closed_has_timestamp"),
            models.CheckConstraint(
                condition=~models.Q(status="cancelled") | (models.Q(cancelled_at__isnull=False) & ~models.Q(cancel_reason="")),
                name="finding_cancelled_has_reason",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status", "due_at"]),
            models.Index(fields=["organization", "location", "status"]),
            models.Index(fields=["organization", "severity", "status"]),
            models.Index(fields=["organization", "category"]),
            models.Index(fields=["organization", "detected_at"]),
            models.Index(fields=["assigned_to", "status"]),
            models.Index(fields=["execution", "item"]),
        ]

    def __str__(self) -> str:
        return f"{self.reference} {self.title}"

    @property
    def reference(self) -> str:
        return f"FND-{self.number:05d}"

    def get_absolute_url(self) -> str:
        from django.urls import reverse

        return reverse("findings:detail", args=[self.pk])

    @property
    def is_active_work(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def is_overdue(self) -> bool:
        """Derived on every read, never stored: work still needed past its due date."""
        return bool(self.is_active_work and self.due_at and self.due_at < timezone.now())

    @property
    def effective_status(self) -> str:
        return OVERDUE if self.is_overdue else self.status
