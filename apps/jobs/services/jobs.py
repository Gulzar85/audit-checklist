"""Creating, cancelling and retrying jobs (permissioned, audited).  The runner (``runner.py``) is the only code that claims and executes them."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone

from .. import constants as k
from .. import policies, registry
from ..errors import JobConflict, JobError
from ..models import ScheduleDefinition, ScheduledJob
from .base import VERBS, audit, log_job


def enqueue(task_code: str, *, organization: Any = None, payload: Any = None, trigger: str = k.Trigger.SYSTEM, actor: Any = None, scheduled_for: datetime | None = None,
            available_at: datetime | None = None, schedule: ScheduleDefinition | None = None, idempotency_key: str = "", priority: int = 5, max_attempts: int | None = None,
            status: str = k.JobStatus.PENDING, now: datetime | None = None) -> tuple[ScheduledJob, bool]:
    """Create a job (no permission check: callers - the runner, ``create_manual`` - decide who may).  Returns ``(job, created)``; the same ``idempotency_key`` returns the existing job."""
    now = now or timezone.now()
    task = registry.require(task_code)
    if task.scope == registry.SCOPE_GLOBAL and organization is not None:
        raise JobError("That task is platform-wide.", "scope_mismatch")
    if task.scope == registry.SCOPE_ORGANIZATION and organization is None:
        raise JobError("That task needs an organization.", "scope_mismatch")
    clean = registry.clean_payload(task, payload)
    if len(json.dumps(clean)) > k.MAX_PAYLOAD_BYTES:
        raise JobError("The payload is too large.", "invalid_payload")
    key = (idempotency_key or f"adhoc:{uuid.uuid4().hex}")[:120]
    existing = ScheduledJob.objects.filter(idempotency_key=key).first()
    if existing is not None:
        return existing, False
    when = scheduled_for or now
    try:
        with transaction.atomic():
            job = ScheduledJob.objects.create(organization=organization, schedule=schedule, task_type=task.code, payload=clean, status=status, trigger=trigger, scheduled_for=when,
                                              available_at=available_at or when, priority=max(0, min(int(priority), 9)), max_attempts=registry.retry_limits(task, max_attempts),
                                              idempotency_key=key, created_by=actor if getattr(actor, "is_authenticated", False) else None)
    except IntegrityError:  # a concurrent creator won the race on the same key
        return ScheduledJob.objects.get(idempotency_key=key), False
    return job, True


def create_manual(actor: Any, task_code: str, organization: Any, payload: Any = None, *, delay_seconds: int = 0, schedule: ScheduleDefinition | None = None, client_key: str = "",
                  now: datetime | None = None) -> ScheduledJob:
    """"Run now" (or run after a delay) a permitted task.  Needs the task's permission in the organization; a repeated ``client_key`` returns the first job."""
    now = now or timezone.now()
    task = registry.require(task_code)
    if not policies.can_trigger(actor, task, organization):
        raise PermissionDenied
    if isinstance(delay_seconds, bool) or not isinstance(delay_seconds, int) or not 0 <= delay_seconds <= k.MAX_DELAY_SECONDS:
        raise JobError("The delay is out of range.", "invalid_delay")
    key = f"manual:{getattr(organization, 'pk', 'global')}:{task.code}:{client_key[:60]}" if client_key else ""
    job, created = enqueue(task.code, organization=organization, payload=payload, trigger=k.Trigger.MANUAL, actor=actor, schedule=schedule, idempotency_key=key,
                           available_at=now + timezone.timedelta(seconds=delay_seconds), now=now)
    if created:
        audit(VERBS["manual_run"], organization, actor, job, f"{task.name} queued by hand", task_type=task.code, delay_seconds=delay_seconds, correlation_id=job.correlation_id)
        _after_enqueue(job)
    return job


def _after_enqueue(job: ScheduledJob) -> None:
    """Optionally hand an immediate job to a Django Tasks backend that has a real worker.  The default ("poll") does nothing: the runner command picks the job up."""
    if getattr(settings, "JOBS_EXECUTION_BACKEND", "poll") != "django_tasks":
        return
    from ..tasks import run_job

    transaction.on_commit(lambda: run_job.enqueue(str(job.pk)))


def _guard(job: ScheduledJob, actor: Any) -> None:
    task = registry.get(job.task_type)
    if not policies.can_run(actor, job.organization) or (task is not None and task.scope == registry.SCOPE_GLOBAL and not policies.can_trigger(actor, task, None)):
        raise PermissionDenied


def _reason(value: Any) -> str:
    text = str(value or "").strip()
    if not 3 <= len(text) <= 300:
        raise JobError("Give a reason (3 to 300 characters).", "reason_required")
    return text


def cancel(job: ScheduledJob, actor: Any, reason: str, *, now: datetime | None = None) -> ScheduledJob:
    """Cancel a job that has not started.  The update is conditional, so a job a runner has just started is never cancelled half-way (it finishes; handlers are idempotent)."""
    _guard(job, actor)
    reason = _reason(reason)
    now = now or timezone.now()
    changed = ScheduledJob.objects.filter(pk=job.pk, status__in=list(k.CANCELLABLE)).update(
        status=k.JobStatus.CANCELLED, cancelled_at=now, cancelled_by=actor, cancel_reason=reason, finished_at=now, lease_token="", lease_expires_at=None)
    job.refresh_from_db()
    if not changed:
        raise JobConflict("This job has already started or finished, so it cannot be cancelled.", "not_cancellable")
    audit(VERBS["cancelled"], job.organization, actor, job, "Job cancelled", reason=reason, task_type=job.task_type, correlation_id=job.correlation_id)
    return job


def retry(job: ScheduledJob, actor: Any, reason: str, *, now: datetime | None = None) -> ScheduledJob:
    """Manual retry of a *failed* job: a fresh attempt allowance, the same correlation id and the same idempotency protections in the handler."""
    _guard(job, actor)
    reason = _reason(reason)
    now = now or timezone.now()
    if registry.get(job.task_type) is None:
        raise JobConflict("That task type is no longer available.", "unknown_task")
    changed = ScheduledJob.objects.filter(pk=job.pk, status=k.JobStatus.FAILED).update(
        status=k.JobStatus.PENDING, attempt_count=0, available_at=now, finished_at=None, started_at=None, duration_ms=None, error_category="", error_message="")
    job.refresh_from_db()
    if not changed:
        raise JobConflict("Only a failed job can be retried.", "not_retryable")
    audit(VERBS["manual_retry"], job.organization, actor, job, "Job retried by hand", reason=reason, task_type=job.task_type, correlation_id=job.correlation_id)
    log_job("manual_retry", job)
    return job
