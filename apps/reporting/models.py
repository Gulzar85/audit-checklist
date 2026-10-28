"""Reporting models (Phase 19): only configuration and run *metadata* - report rows are never stored.

* ``SavedReport`` - a validated configuration (report code, filters, columns, sort, grouping).  It stores *what to ask*, never an authorization: it is validated and
  authorized again every time it runs.
* ``ReportRun``   - the record of one generation: who asked, the normalized parameters, status, row count, timing, and - for exports - a *private artifact* reference.
  Retrieval re-checks the requester's current permission; a run is not a standing grant.
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


class SavedReport(BaseModel):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="saved_reports")
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="saved_reports")
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=500, blank=True)
    report_code = models.CharField(max_length=60)
    report_version = models.PositiveSmallIntegerField(default=1)
    filters = models.JSONField(default=dict, blank=True)
    columns = models.JSONField(default=list, blank=True)
    sort = models.CharField(max_length=60, blank=True)
    group_by = models.CharField(max_length=30, blank=True)
    default_format = models.CharField(max_length=4, choices=k.Format.choices, blank=True)
    visibility = models.CharField(max_length=14, choices=k.Visibility.choices, default=k.Visibility.PRIVATE)
    history = HistoricalRecords()

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "owner", "name"], name="saved_report_unique_name"),
            models.CheckConstraint(condition=models.Q(visibility__in=k.Visibility.values), name="saved_report_visibility_valid"),
        ]
        indexes = [models.Index(fields=["organization", "visibility"], name="saved_report_org_idx"), models.Index(fields=["owner"], name="saved_report_owner_idx")]

    def __str__(self) -> str:
        return self.name


class ReportRun(UUIDModel):
    created_at = models.DateTimeField(auto_now_add=True)
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="report_runs")
    requested_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name="report_runs")
    origin = models.CharField(max_length=10, choices=k.Origin.choices, default=k.Origin.USER)
    saved_report = models.ForeignKey(SavedReport, on_delete=models.SET_NULL, null=True, blank=True, related_name="runs")
    report_code = models.CharField(max_length=60)
    report_version = models.PositiveSmallIntegerField(default=1)
    params = models.JSONField(default=dict, blank=True, help_text="The normalized request (filters, columns, sort, grouping) - no result rows.")
    format = models.CharField(max_length=4, choices=k.Format.choices)
    status = models.CharField(max_length=10, choices=k.RunStatus.choices, default=k.RunStatus.QUEUED)
    row_count = models.PositiveIntegerField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    correlation_id = models.CharField(max_length=32, default=new_correlation_id)
    request_hash = models.CharField(max_length=64, help_text="Hash of (user, organization, report, normalized params, format): identical requests share it.")
    idempotency_key = models.CharField(max_length=64)
    error_message = models.CharField(max_length=200, blank=True)
    artifact_name = models.CharField(max_length=80, blank=True, help_text="Random private file name under REPORT_ARTIFACT_ROOT; never exposed in a URL.")
    artifact_size = models.PositiveIntegerField(null=True, blank=True)
    artifact_sha256 = models.CharField(max_length=64, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    job = models.ForeignKey("jobs.ScheduledJob", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["idempotency_key"], name="report_run_unique_key"),
            models.CheckConstraint(condition=models.Q(status__in=k.RunStatus.values), name="report_run_status_valid"),
            models.CheckConstraint(condition=models.Q(format__in=k.Format.values), name="report_run_format_valid"),
        ]
        indexes = [models.Index(fields=["requested_by", "-created_at"], name="report_run_user_idx"), models.Index(fields=["organization", "-created_at"], name="report_run_org_idx"),
                   models.Index(fields=["status", "expires_at"], name="report_run_expiry_idx"), models.Index(fields=["request_hash", "-created_at"], name="report_run_hash_idx")]

    def __str__(self) -> str:
        return f"{self.report_code} {self.status}"


