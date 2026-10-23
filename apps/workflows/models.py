"""Workflow domain models (Phase 14).

* ``WorkflowDefinition`` - the stable identity (organization-specific, or system-wide when ``organization`` is null); one *default*
  per target type, an organization default taking precedence over the system one.
* ``WorkflowVersion`` - independently versioned configuration (draft -> published -> superseded / archived).  Published versions are
  immutable: states, transitions and the version itself are protected by database triggers (``guards.py``).
* ``WorkflowState`` / ``WorkflowTransition`` - the configured state machine.  Approval stages are *states* with an assignee policy and
  an approval mode; there is deliberately no separate "step" entity (ADR 0069).
* ``WorkflowInstance`` - a workflow running against one business record (a registered target, never a client-chosen model), bound to the
  exact version it started with.
* ``WorkflowTask`` - work for a user or a claimable group; ``WorkflowEvent`` - the append-only decision / transition history (also the
  durable idempotency and approval record).  The activity log receives the audit entries as in every other module.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone
from simple_history.models import HistoricalRecords

from apps.core.models import BaseModel

from .constants import (
    OPEN_TASK_STATUSES, TERMINAL_KINDS, ActorRule, ApprovalMode, Decision, DefinitionStatus, EventKind, InstanceStatus, OnNoAssignee, Outcome, StateKind,
    TaskStatus, VersionStatus,
)

User = settings.AUTH_USER_MODEL


class WorkflowDefinition(BaseModel):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, null=True, blank=True, related_name="workflow_definitions",
                                     help_text="Empty for a system-defined workflow available to every organization.")
    code = models.SlugField(max_length=60)
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=500, blank=True)
    #: A key of the target registry (``targets.py``): which kind of business record the workflow governs.
    target_type = models.CharField(max_length=40)
    #: Used when a record of ``target_type`` enters a workflow without naming one.  An organization default overrides the system default.
    is_default = models.BooleanField(default=False)
    status = models.CharField(max_length=10, choices=DefinitionStatus.choices, default=DefinitionStatus.ACTIVE)
    #: A controlled event of the target type that starts the workflow automatically (empty = started explicitly).
    auto_start_event = models.CharField(max_length=60, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    history = HistoricalRecords()

    class Meta:
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], condition=models.Q(organization__isnull=False), name="unique_workflow_code_per_organization"),
            models.UniqueConstraint(fields=["code"], condition=models.Q(organization__isnull=True), name="unique_system_workflow_code"),
            models.UniqueConstraint(fields=["organization", "target_type"], condition=models.Q(is_default=True, status="active", organization__isnull=False),
                                    name="one_default_workflow_per_organization_target"),
            models.UniqueConstraint(fields=["target_type"], condition=models.Q(is_default=True, status="active", organization__isnull=True),
                                    name="one_default_system_workflow_per_target"),
            models.CheckConstraint(condition=models.Q(status__in=[s for s, _l in DefinitionStatus.choices]), name="workflow_definition_status_valid"),
        ]
        indexes = [models.Index(fields=["organization", "target_type", "status"])]

    def __str__(self) -> str:
        return self.name

    @property
    def is_system(self) -> bool:
        return self.organization_id is None

    def get_absolute_url(self) -> str:
        from django.urls import reverse

        return reverse("workflows:detail", args=[self.pk])


class WorkflowVersion(BaseModel):
    definition = models.ForeignKey(WorkflowDefinition, on_delete=models.PROTECT, related_name="versions")
    number = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=VersionStatus.choices, default=VersionStatus.DRAFT, db_index=True)
    change_summary = models.CharField(max_length=300, blank=True)
    #: A Phase 6-style condition tree over the target type's registered attributes: the record may enter the workflow only when it is TRUE.
    start_condition = models.JSONField(null=True, blank=True)
    #: Cycles are rejected at publication unless the designer explicitly allows them (e.g. return-for-correction loops).
    allow_loops = models.BooleanField(default=False)
    #: SHA-256 of the canonical configuration, stored at publication: proof the running version is the one that was reviewed.
    config_hash = models.CharField(max_length=64, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    published_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    published_at = models.DateTimeField(null=True, blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ("definition", "-number")
        constraints = [
            models.UniqueConstraint(fields=["definition", "number"], name="unique_workflow_version_number"),
            models.UniqueConstraint(fields=["definition"], condition=models.Q(status="draft"), name="one_draft_workflow_version"),
            models.UniqueConstraint(fields=["definition"], condition=models.Q(status="published"), name="one_published_workflow_version"),
            models.CheckConstraint(condition=models.Q(status__in=[s for s, _l in VersionStatus.choices]), name="workflow_version_status_valid"),
            models.CheckConstraint(condition=models.Q(status="draft") | (models.Q(published_at__isnull=False) & ~models.Q(config_hash="")), name="workflow_version_published_is_sealed"),
        ]

    def __str__(self) -> str:
        return f"{self.definition.code} v{self.number}"

    @property
    def is_draft(self) -> bool:
        return self.status == VersionStatus.DRAFT

    def get_absolute_url(self) -> str:
        from django.urls import reverse

        return reverse("workflows:version", args=[self.pk])


class WorkflowState(BaseModel):
    version = models.ForeignKey(WorkflowVersion, on_delete=models.PROTECT, related_name="states")
    code = models.SlugField(max_length=40)
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=300, blank=True)
    kind = models.CharField(max_length=14, choices=StateKind.choices, default=StateKind.INTERMEDIATE)
    sort_order = models.PositiveIntegerField(default=0)
    # -- tasks created when the state is entered (empty policy = a waiting state with no task) -------------------------------------
    assignee_policy = models.JSONField(null=True, blank=True, help_text='{"type": "user"|"permission"|"role", ...}')
    approval_mode = models.CharField(max_length=4, choices=ApprovalMode.choices, default=ApprovalMode.ONE)
    min_approvals = models.PositiveSmallIntegerField(default=1)
    separation = models.JSONField(default=list, blank=True, help_text="Separation-of-duties options (controlled list).")
    due_hours = models.PositiveIntegerField(null=True, blank=True, help_text="Task deadline, hours after the state is entered.")
    on_no_assignee = models.CharField(max_length=10, choices=OnNoAssignee.choices, default=OnNoAssignee.BLOCK)

    class Meta:
        ordering = ("version", "sort_order", "code")
        constraints = [
            models.UniqueConstraint(fields=["version", "code"], name="unique_workflow_state_code"),
            models.UniqueConstraint(fields=["version"], condition=models.Q(kind="initial"), name="one_initial_workflow_state"),
            models.CheckConstraint(condition=models.Q(kind__in=[k for k, _l in StateKind.choices]), name="workflow_state_kind_valid"),
            models.CheckConstraint(condition=models.Q(approval_mode__in=[m for m, _l in ApprovalMode.choices]), name="workflow_state_mode_valid"),
            models.CheckConstraint(condition=models.Q(min_approvals__gte=1, min_approvals__lte=20), name="workflow_state_min_valid"),
            models.CheckConstraint(condition=models.Q(on_no_assignee__in=[m for m, _l in OnNoAssignee.choices]), name="workflow_state_no_assignee_valid"),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def is_terminal(self) -> bool:
        return self.kind in TERMINAL_KINDS


class WorkflowTransition(BaseModel):
    version = models.ForeignKey(WorkflowVersion, on_delete=models.PROTECT, related_name="transitions")
    action = models.SlugField(max_length=40, help_text="Stable action code (submit, approve, return ...).")
    label = models.CharField(max_length=80)
    source = models.ForeignKey(WorkflowState, on_delete=models.PROTECT, related_name="outgoing")
    target = models.ForeignKey(WorkflowState, on_delete=models.PROTECT, related_name="incoming")
    #: A key of the business-action registry (``handlers.py``) for the version's target type; empty = no domain operation.
    handler = models.CharField(max_length=60, blank=True)
    actor_rule = models.CharField(max_length=12, choices=ActorRule.choices, default=ActorRule.ASSIGNEE)
    decision = models.CharField(max_length=8, choices=Decision.choices, default=Decision.OTHER)
    reason_required = models.BooleanField(default=False)
    #: A Phase 6-style condition over the target's registered attributes.  Several transitions may share (source, action): the first whose condition is
    #: TRUE (an unconditional one is last) is the branch taken; an unknown condition never selects a branch.
    condition = models.JSONField(null=True, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    enabled = models.BooleanField(default=True)

    class Meta:
        ordering = ("version", "source__sort_order", "sort_order", "action")
        constraints = [
            models.UniqueConstraint(fields=["version", "source", "action", "sort_order"], name="unique_workflow_transition_branch"),
            models.CheckConstraint(condition=models.Q(actor_rule__in=[a for a, _l in ActorRule.choices]), name="workflow_transition_actor_valid"),
            models.CheckConstraint(condition=models.Q(decision__in=[d for d, _l in Decision.choices]), name="workflow_transition_decision_valid"),
        ]
        indexes = [models.Index(fields=["version", "source", "action"])]

    def __str__(self) -> str:
        return f"{self.source.code} --{self.action}--> {self.target.code}"


class WorkflowInstance(BaseModel):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, related_name="workflow_instances")
    #: Sequential per organization (``WF-00042``), allocated under the organization row lock.
    number = models.PositiveIntegerField()
    definition = models.ForeignKey(WorkflowDefinition, on_delete=models.PROTECT, related_name="instances")
    #: The exact published version the workflow started with.  Publishing a newer version never changes a running instance.
    version = models.ForeignKey(WorkflowVersion, on_delete=models.PROTECT, related_name="instances")
    target_type = models.CharField(max_length=40)
    #: The business record, resolved only through the target registry (no generic foreign key, no client-chosen model).
    target_id = models.UUIDField()
    location = models.ForeignKey("locations.Location", on_delete=models.PROTECT, related_name="workflow_instances")
    state = models.ForeignKey(WorkflowState, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=10, choices=InstanceStatus.choices, default=InstanceStatus.ACTIVE, db_index=True)
    outcome = models.CharField(max_length=8, choices=Outcome.choices, blank=True)
    needs_attention = models.BooleanField(default=False)
    started_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    system_started = models.BooleanField(default=False)
    started_at = models.DateTimeField(default=timezone.now)
    state_entered_at = models.DateTimeField(default=timezone.now)
    #: Which visit to a state this is (1, 2 ...): tasks, approvals and idempotency are scoped to it, so a loop never reuses stale decisions.
    entry_seq = models.PositiveIntegerField(default=1)
    #: Optimistic-concurrency token of the instance: bumped by every accepted transition.
    revision = models.PositiveIntegerField(default=1)
    completed_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=300, blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ("-started_at",)
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="unique_workflow_instance_number"),
            # One active workflow per business record and definition (ADR 0071); several workflows (different definitions) may run side by side.
            models.UniqueConstraint(fields=["target_type", "target_id", "definition"], condition=models.Q(status="active"), name="one_active_instance_per_target_definition"),
            models.CheckConstraint(condition=models.Q(status__in=[s for s, _l in InstanceStatus.choices]), name="workflow_instance_status_valid"),
            models.CheckConstraint(condition=~models.Q(status="completed") | (models.Q(completed_at__isnull=False) & ~models.Q(outcome="")), name="workflow_instance_completed_has_outcome"),
            models.CheckConstraint(condition=~models.Q(status="cancelled") | (models.Q(cancelled_at__isnull=False) & ~models.Q(cancel_reason="")), name="workflow_instance_cancelled_has_reason"),
        ]
        indexes = [
            models.Index(fields=["organization", "status", "started_at"]),
            models.Index(fields=["target_type", "target_id"]),
            models.Index(fields=["organization", "location", "status"]),
        ]

    def __str__(self) -> str:
        return self.reference

    @property
    def reference(self) -> str:
        return f"WF-{self.number:05d}"

    @property
    def is_active(self) -> bool:
        return self.status == InstanceStatus.ACTIVE

    def get_absolute_url(self) -> str:
        from django.urls import reverse

        return reverse("workflows:instance", args=[self.pk])


class WorkflowTask(BaseModel):
    instance = models.ForeignKey(WorkflowInstance, on_delete=models.PROTECT, related_name="tasks")
    state = models.ForeignKey(WorkflowState, on_delete=models.PROTECT, related_name="+")
    entry_seq = models.PositiveIntegerField()
    #: ``group`` for the one claimable task of a role / permission policy, or the assigned user's id: unique per state entry, so a repeated
    #: event cannot create a duplicate task.
    slot = models.CharField(max_length=40)
    assignee = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="workflow_tasks")
    status = models.CharField(max_length=10, choices=TaskStatus.choices, default=TaskStatus.PENDING, db_index=True)
    due_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    completed_at = models.DateTimeField(null=True, blank=True)
    #: Optimistic-concurrency token for claim / release / reassign.
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ("due_at", "created_at")
        constraints = [
            models.UniqueConstraint(fields=["instance", "entry_seq", "slot"], name="one_task_per_slot_and_entry"),
            models.CheckConstraint(condition=models.Q(status__in=[s for s, _l in TaskStatus.choices]), name="workflow_task_status_valid"),
            models.CheckConstraint(condition=~models.Q(status="claimed") | models.Q(assignee__isnull=False), name="workflow_task_claimed_has_assignee"),
            models.CheckConstraint(condition=~models.Q(status="completed") | models.Q(completed_at__isnull=False), name="workflow_task_completed_has_timestamp"),
        ]
        indexes = [models.Index(fields=["assignee", "status"]), models.Index(fields=["instance", "status"]), models.Index(fields=["status", "due_at"])]

    def __str__(self) -> str:
        return f"{self.instance_id}:{self.slot}"

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_TASK_STATUSES

    @property
    def is_overdue(self) -> bool:
        """Derived on every read (ADR 0072): an open task past its due date.  No job is needed for correctness."""
        return bool(self.is_open and self.due_at and self.due_at < timezone.now())


class WorkflowEvent(BaseModel):
    """The append-only record of everything that happened to an instance: transitions, approvals, task events, start and cancellation.

    It is also the durable idempotency record (``UNIQUE(instance, idempotency_key)``) and the approval ledger (one approval per actor and state
    entry).  The activity log carries the same events for the platform-wide audit trail; this table is what the engine itself reasons from.
    """

    instance = models.ForeignKey(WorkflowInstance, on_delete=models.PROTECT, related_name="events")
    seq = models.PositiveIntegerField()
    kind = models.CharField(max_length=16, choices=EventKind.choices)
    action = models.CharField(max_length=40, blank=True)
    from_state = models.ForeignKey(WorkflowState, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    to_state = models.ForeignKey(WorkflowState, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    task = models.ForeignKey(WorkflowTask, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    entry_seq = models.PositiveIntegerField(default=1)
    comment = models.CharField(max_length=1000, blank=True)
    idempotency_key = models.UUIDField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("instance", "seq")
        constraints = [
            models.UniqueConstraint(fields=["instance", "seq"], name="unique_workflow_event_seq"),
            models.UniqueConstraint(fields=["instance", "idempotency_key"], condition=models.Q(idempotency_key__isnull=False), name="one_event_per_idempotency_key"),
            models.UniqueConstraint(fields=["instance", "entry_seq", "actor"], condition=models.Q(kind="approval"), name="one_approval_per_actor_and_entry"),
            models.CheckConstraint(condition=models.Q(kind__in=[k for k, _l in EventKind.choices]), name="workflow_event_kind_valid"),
        ]
        indexes = [models.Index(fields=["instance", "kind", "entry_seq"])]

    def __str__(self) -> str:
        return f"{self.instance_id}#{self.seq} {self.kind}"
