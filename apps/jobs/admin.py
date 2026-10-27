"""Django admin for scheduling: read-only inspection.  Changes go through the services (validation, permissions, audit, no duplicate runs)."""

from __future__ import annotations

from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from . import selectors
from .models import JobExecution, RunnerHeartbeat, ScheduleDefinition, ScheduledJob


class ReadOnlyMixin:
    actions = None

    def has_add_permission(self, request) -> bool:  # noqa: ANN001
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False


@admin.register(ScheduleDefinition)
class ScheduleDefinitionAdmin(ReadOnlyMixin, SimpleHistoryAdmin):
    list_display = ("name", "task_type", "schedule_type", "status", "organization", "next_run_at", "last_run_at")
    list_filter = ("status", "schedule_type", "task_type")
    search_fields = ("name",)
    list_select_related = ("organization",)
    exclude = ("payload",)

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        return qs if request.user.is_superuser else qs.filter(pk__in=selectors.schedules_visible_to(request.user).values("pk"))


@admin.register(ScheduledJob)
class ScheduledJobAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ("task_type", "status", "organization", "scheduled_for", "attempt_count", "duration_ms")
    list_filter = ("status", "task_type", "trigger")
    search_fields = ("correlation_id",)
    list_select_related = ("organization",)
    date_hierarchy = "scheduled_for"
    exclude = ("payload", "lease_token")

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        return qs if request.user.is_superuser else qs.filter(pk__in=selectors.jobs_visible_to(request.user).values("pk"))


@admin.register(JobExecution)
class JobExecutionAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ("job", "number", "outcome", "duration_ms", "started_at")
    list_filter = ("outcome",)
    list_select_related = ("job",)

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        return qs if request.user.is_superuser else qs.filter(job__in=selectors.jobs_visible_to(request.user).values("pk"))


@admin.register(RunnerHeartbeat)
class RunnerHeartbeatAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ("runner", "last_seen_at", "jobs_processed")

    def get_queryset(self, request):  # noqa: ANN001, ANN201
        qs = super().get_queryset(request)
        return qs if request.user.is_superuser else qs.none()
