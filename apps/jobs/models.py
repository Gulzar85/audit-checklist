"""Scheduling models (Phase 18).

* ``ScheduleDefinition`` - a *recurring or one-time rule* ("run task X at this time"): one row per rule, never one row per future occurrence.
* ``ScheduledJob``       - one concrete execution request.  Created when an occurrence becomes due (the runner materializes it), on "run now", or for a manual retry.
* ``JobExecution``       - one attempt of a job: the history of retries and failures.
* ``RunnerHeartbeat``    - when a runner last looked at the queue, so a missing runner is distinguishable from an idle queue.

``organization`` is ``NULL`` only for platform-wide schedules and jobs (superusers).  A job never trusts an organization (or anything else) inside its payload.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import BaseModel, UUIDModel

from . import constants as k

User = settings.AUTH_USER_MODEL


def new_correlation_id() -> str:
    return uuid.uuid4().hex


class ScheduleDefinition(BaseModel):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, null=True, blank=True, related_name="job_schedules",
                                     help_text="Empty for a platform-wide schedule (superusers only).")
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=500, blank=True)
    task_type = models.CharField(max_length=60, help_text="A code from the server-side task registry.")
    schedule_type = models.CharField(max_length=10, choices=k.ScheduleType.choices)
    status = models.CharField(max_length=10, choices=k.ScheduleStatus.choices, default=k.ScheduleStatus.ACTIVE)
    timezone = models.CharField(max_length=64, default="UTC", help_text="The IANA zone the recurrence is expressed in; changing it is an explicit, audited act.")
    start_at = models.DateTimeField(help_text="UTC. The run time of a one-time schedule; the first possible run (and the anchor of an interval) otherwise.")
    end_at = models.DateTimeField(null=True, blank=True)
    recurrence = models.JSONField(default=dict, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    misfire_policy = models.CharField(max_length=10, choices=k.MisfirePolicy.choices, default=k.MisfirePolicy.SKIP)
    max_attempts = models.PositiveSmallIntegerField(default=3)
    next_run_at = models.DateTimeField(null=True, blank=True, help_text="UTC. The next occurrence still to be materialized; empty when paused, disabled or finished.")
    last_run_at = models.DateTimeField(null=True, blank=True)
    revision = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    history = HistoricalRecords()

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], condition=models.Q(organization__isnull=False), name="job_schedule_unique_name_org"),
            models.UniqueConstraint(fields=["name"], condition=models.Q(organization__isnull=True), name="job_schedule_unique_name_global"),
            models.CheckConstraint(condition=models.Q(status__in=k.ScheduleStatus.values), name="job_schedule_status_valid"),
            models.CheckConstraint(condition=models.Q(schedule_type__in=k.ScheduleType.values), name="job_schedule_type_valid"),
            models.CheckConstraint(condition=models.Q(misfire_policy__in=k.MisfirePolicy.values), name="job_schedule_misfire_valid"),
            models.CheckConstraint(condition=models.Q(next_run_at__isnull=True) | models.Q(status="active"), name="job_schedule_next_only_when_active"),
            models.CheckConstraint(condition=models.Q(end_at__isnull=True) | models.Q(end_at__gt=models.F("start_at")), name="job_schedule_end_after_start"),
            models.CheckConstraint(condition=models.Q(max_attempts__gte=1, max_attempts__lte=k.MAX_ATTEMPTS_LIMIT), name="job_schedule_attempts_valid"),
        ]
        indexes = [models.Index(fields=["status", "next_run_at"], name="job_schedule_due_idx"), models.Index(fields=["organization", "status"], name="job_schedule_org_idx")]

    def __str__(self) -> str:
        return self.name


class ScheduledJob(UUIDModel):
    created_at = models.DateTimeField(auto_now_add=True)
    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, null=True, blank=True, related_name="scheduled_jobs")
    schedule = models.ForeignKey(ScheduleDefinition, on_delete=models.SET_NULL, null=True, blank=True, related_name="jobs")
    task_type = models.CharField(max_length=60)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=14, choices=k.JobStatus.choices, default=k.JobStatus.PENDING)
    trigger = models.CharField(max_length=10, choices=k.Trigger.choices, default=k.Trigger.SCHEDULE)
    scheduled_for = models.DateTimeField(help_text="The occurrence this job stands for (UTC).")
    available_at = models.DateTimeField(help_text="Not claimed before this instant (UTC): the scheduled time, or the next retry time.")
    priority = models.PositiveSmallIntegerField(default=5)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3)
    idempotency_key = models.CharField(max_length=120)
    correlation_id = models.CharField(max_length=32, default=new_correlation_id)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    claimed_by = models.CharField(max_length=60, blank=True)
    lease_token = models.CharField(max_length=40, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    result = models.JSONField(default=dict, blank=True)
    error_category = models.CharField(max_length=30, blank=True)
    error_message = models.CharField(max_length=k.ERROR_LIMIT, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    cancel_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-scheduled_for", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["idempotency_key"], name="scheduled_job_unique_key"),
            models.CheckConstraint(condition=models.Q(status__in=k.JobStatus.values), name="scheduled_job_status_valid"),
            models.CheckConstraint(condition=models.Q(trigger__in=k.Trigger.values), name="scheduled_job_trigger_valid"),
            models.CheckConstraint(condition=models.Q(priority__lte=9), name="scheduled_job_priority_valid"),
            models.CheckConstraint(condition=models.Q(attempt_count__gte=0, max_attempts__gte=1), name="scheduled_job_attempts_valid"),
            models.CheckConstraint(condition=~models.Q(status__in=["claimed", "running"]) | (~models.Q(lease_token="") & models.Q(lease_expires_at__isnull=False)), name="scheduled_job_lease_present"),
        ]
        indexes = [
            models.Index(fields=["status", "available_at", "-priority"], name="scheduled_job_claim_idx"),
            models.Index(fields=["status", "lease_expires_at"], name="scheduled_job_lease_idx"),
            models.Index(fields=["organization", "status", "-created_at"], name="scheduled_job_org_idx"),
            models.Index(fields=["schedule", "-scheduled_for"], name="scheduled_job_schedule_idx"),
            models.Index(fields=["finished_at"], name="scheduled_job_finished_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.task_type} {self.status}"

    @property
    def is_open(self) -> bool:
        return self.status in k.OPEN


class JobExecution(UUIDModel):
    created_at = models.DateTimeField(auto_now_add=True)
    job = models.ForeignKey(ScheduledJob, on_delete=models.CASCADE, related_name="executions")
    number = models.PositiveSmallIntegerField()
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField()
    outcome = models.CharField(max_length=20, choices=k.ExecutionOutcome.choices)
    duration_ms = models.PositiveIntegerField(default=0)
    error_category = models.CharField(max_length=30, blank=True)
    error_message = models.CharField(max_length=k.ERROR_LIMIT, blank=True)
    retry_decision = models.CharField(max_length=80, blank=True)
    correlation_id = models.CharField(max_length=32)
    runner = models.CharField(max_length=60, blank=True)
    soft_limit_exceeded = models.BooleanField(default=False)

    class Meta:
        ordering = ["job_id", "number"]
        constraints = [
            models.UniqueConstraint(fields=["job", "number"], name="job_execution_unique_number"),
            models.CheckConstraint(condition=models.Q(outcome__in=k.ExecutionOutcome.values), name="job_execution_outcome_valid"),
        ]
        indexes = [models.Index(fields=["created_at"], name="job_execution_created_idx"), models.Index(fields=["outcome", "-created_at"], name="job_execution_outcome_idx")]


class RunnerHeartbeat(models.Model):
    runner = models.CharField(max_length=60, primary_key=True)
    last_seen_at = models.DateTimeField()
    last_started_at = models.DateTimeField(null=True, blank=True)
    jobs_processed = models.PositiveIntegerField(default=0)
    last_summary = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return self.runner
