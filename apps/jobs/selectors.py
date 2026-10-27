"""Read access: every query is scoped to what the user may see (their organizations; platform-wide rows only for superusers)."""

from __future__ import annotations

from typing import Any

from django.db.models import Count, Q, QuerySet
from django.utils import timezone

from apps.core.policies import is_active_user
from apps.core.utils import parse_uuid
from apps.permissions.authorization import get_accessible_organizations
from apps.permissions.registry import P

from . import constants as k
from .models import JobExecution, RunnerHeartbeat, ScheduleDefinition, ScheduledJob


def _scope(user: Any, field: str = "organization") -> Q | None:
    """``None`` = nothing visible; ``Q()`` = everything (superuser)."""
    if not is_active_user(user):
        return None
    if getattr(user, "is_superuser", False):
        return Q()
    ids = list(get_accessible_organizations(user, P.JOBS_VIEW).values_list("pk", flat=True))
    return Q(**{f"{field}__in": ids}) if ids else None


def schedules_visible_to(user: Any) -> QuerySet[ScheduleDefinition]:
    scope = _scope(user)
    return ScheduleDefinition.objects.none() if scope is None else ScheduleDefinition.objects.filter(scope).select_related("organization")


def get_schedule(user: Any, pk: Any) -> ScheduleDefinition | None:
    parsed = parse_uuid(pk)
    return schedules_visible_to(user).filter(pk=parsed).first() if parsed else None


def jobs_visible_to(user: Any) -> QuerySet[ScheduledJob]:
    scope = _scope(user)
    return ScheduledJob.objects.none() if scope is None else ScheduledJob.objects.filter(scope).select_related("organization", "schedule")


def get_job(user: Any, pk: Any) -> ScheduledJob | None:
    parsed = parse_uuid(pk)
    return jobs_visible_to(user).filter(pk=parsed).first() if parsed else None


def executions_of(job: ScheduledJob) -> QuerySet[JobExecution]:
    return JobExecution.objects.filter(job=job).order_by("number")


def job_counts(user: Any) -> dict[str, int]:
    rows = jobs_visible_to(user).values("status").annotate(n=Count("pk"))
    return {row["status"]: row["n"] for row in rows}


def dashboard(user: Any, now: Any = None) -> dict[str, Any]:
    """Real numbers for the dashboard (one grouped query per list; nothing hard-coded)."""
    now = now or timezone.now()
    counts = job_counts(user)
    schedules = schedules_visible_to(user)
    return {
        "counts": counts,
        "pending": counts.get(k.JobStatus.PENDING, 0), "running": counts.get(k.JobStatus.RUNNING, 0) + counts.get(k.JobStatus.CLAIMED, 0),
        "succeeded": counts.get(k.JobStatus.SUCCEEDED, 0), "failed": counts.get(k.JobStatus.FAILED, 0), "retry": counts.get(k.JobStatus.RETRY_PENDING, 0),
        "paused": schedules.filter(status=k.ScheduleStatus.PAUSED).count(),
        "recent_failures": list(jobs_visible_to(user).filter(status=k.JobStatus.FAILED).order_by("-finished_at")[:8]),
        "upcoming": list(schedules.filter(status=k.ScheduleStatus.ACTIVE, next_run_at__isnull=False).order_by("next_run_at")[:8]),
        "recent_success": list(jobs_visible_to(user).filter(status=k.JobStatus.SUCCEEDED).order_by("-finished_at")[:5]),
    }


def diagnostics(now: Any = None) -> dict[str, Any]:
    """Platform-wide runner health (superusers only - the caller checks): a missing heartbeat is reported separately from an idle queue."""
    now = now or timezone.now()
    beat = RunnerHeartbeat.objects.order_by("-last_seen_at").first()
    stale_cut = now - timezone.timedelta(seconds=k.HEARTBEAT_STALE_SECONDS)
    overdue_cut = now - timezone.timedelta(seconds=k.HEARTBEAT_STALE_SECONDS)
    waiting = ScheduledJob.objects.filter(status__in=[k.JobStatus.PENDING, k.JobStatus.RETRY_PENDING], available_at__lte=now)
    if beat is None:
        runner = "never_seen"
    elif beat.last_seen_at < stale_cut:
        runner = "stale"
    else:
        runner = "alive"
    return {
        "runner_state": runner, "last_seen_at": beat.last_seen_at if beat else None, "last_runner": beat.runner if beat else "", "last_summary": beat.last_summary if beat else {},
        "waiting_now": waiting.count(), "waiting_overdue": waiting.filter(available_at__lte=overdue_cut).count(),
        "expired_leases": ScheduledJob.objects.filter(status__in=[k.JobStatus.CLAIMED, k.JobStatus.RUNNING], lease_expires_at__lt=now).count(),
        "retry_pending": ScheduledJob.objects.filter(status=k.JobStatus.RETRY_PENDING).count(),
        "overdue_schedules": ScheduleDefinition.objects.filter(status=k.ScheduleStatus.ACTIVE, next_run_at__lte=overdue_cut).count(),
        "repeated_failures": list(ScheduledJob.objects.filter(status=k.JobStatus.FAILED, finished_at__gte=now - timezone.timedelta(days=1)).values("task_type").annotate(n=Count("pk")).filter(n__gte=3)),
        "recent_success": ScheduledJob.objects.filter(status=k.JobStatus.SUCCEEDED).order_by("-finished_at").values_list("task_type", "finished_at")[:5],
    }
