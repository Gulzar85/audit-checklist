"""The runner (Phase 18): materialize due occurrences, recover expired leases, claim, execute, record.

Concurrency model (PostgreSQL, several runners at once):
* **Materialization** locks each due schedule row (``FOR UPDATE SKIP LOCKED``), creates the occurrence's job under a unique idempotency key
  (``sched:<schedule>:<occurrence>``) and advances ``next_run_at`` in the same transaction: an occurrence can never be created twice.
* **Claiming** locks eligible job rows with ``FOR UPDATE SKIP LOCKED`` and stamps a *lease* (a random token + an expiry).  Runners claim different rows.
* **Starting and finishing** are *conditional updates* on ``(pk, status, lease_token)``: a runner whose lease was recovered, or whose job was cancelled, matches zero rows and
  changes nothing - it can never overwrite a newer attempt.
* **No transaction is open while a handler runs** (the handler may call external services); every state change is its own short transaction.
* Execution is **at-least-once** (a crash after a handler's side effect but before the result is recorded re-runs it), so handlers are idempotent by contract.
"""

from __future__ import annotations

import os
import random
import socket
import time
import uuid
from datetime import datetime
from typing import Any, Callable

from django.conf import settings
from django.db import OperationalError, transaction
from django.db.models import F
from django.utils import timezone

from .. import constants as k
from .. import registry
from ..errors import JobError, PermanentError, RetryableError
from ..models import JobExecution, RunnerHeartbeat, ScheduleDefinition, ScheduledJob
from . import jobs
from .base import VERBS, audit, log_job
from .. import recurrence


def runner_id() -> str:
    return (getattr(settings, "JOBS_RUNNER_ID", "") or f"{socket.gethostname()}:{os.getpid()}")[:60]


def sanitize(message: Any) -> str:
    """Single line, bounded; callers pass only messages they wrote (never raw exception text from a provider)."""
    return " ".join(str(message).split())[: k.ERROR_LIMIT]


def backoff_seconds(task: registry.Task, attempt: int) -> int:
    """``base * 2^(attempt-1)``, capped, with +-``JOBS_RETRY_JITTER`` so retrying jobs do not move in lock step."""
    delay = min(task.base_delay_seconds * (2 ** max(0, attempt - 1)), task.max_delay_seconds, k.MAX_RETRY_DELAY_SECONDS)
    jitter = float(getattr(settings, "JOBS_RETRY_JITTER", 0.1))
    if jitter > 0:
        delay *= 1 + random.uniform(-jitter, jitter)  # noqa: S311 - scheduling jitter, not security
    return max(1, min(int(delay), k.MAX_RETRY_DELAY_SECONDS))


# ----------------------------------------------------------------------------------------------------------------- materialize
def materialize_due(now: datetime | None = None, limit: int = 100) -> dict[str, int]:
    """Turn every due schedule occurrence into a job (or a recorded skip) and advance the schedule.  Bounded by ``limit`` schedules."""
    now = now or timezone.now()
    grace = int(getattr(settings, "JOBS_MISFIRE_GRACE_SECONDS", 300))
    result = {"created": 0, "skipped": 0, "finished": 0}
    due = list(ScheduleDefinition.objects.filter(status=k.ScheduleStatus.ACTIVE, next_run_at__lte=now).order_by("next_run_at").values_list("pk", flat=True)[: max(1, min(limit, k.MAX_BATCH))])
    for pk in due:
        with transaction.atomic():
            schedule = ScheduleDefinition.objects.select_for_update(skip_locked=True).filter(pk=pk, status=k.ScheduleStatus.ACTIVE, next_run_at__lte=now).first()
            if schedule is None:  # another runner is handling it, or it changed
                continue
            occurrence = schedule.next_run_at
            late = (now - occurrence).total_seconds()
            key = f"sched:{schedule.pk}:{occurrence.isoformat()}"
            overlap = ScheduledJob.objects.filter(schedule=schedule, status__in=list(k.OPEN)).exists()
            reason = ""
            if overlap:
                reason = "overlap"
            elif late > grace and schedule.misfire_policy == k.MisfirePolicy.SKIP:
                reason = "misfire"
            if registry.get(schedule.task_type) is None:
                reason = "unknown_task"
            if reason:
                job, created = jobs.enqueue(schedule.task_type, organization=schedule.organization, payload=schedule.payload, trigger=k.Trigger.SCHEDULE, schedule=schedule, idempotency_key=key,
                                            scheduled_for=occurrence, status=k.JobStatus.SKIPPED, now=now) if reason != "unknown_task" else (None, False)
                if job is not None and created:
                    ScheduledJob.objects.filter(pk=job.pk).update(finished_at=now, error_category=reason, error_message={"overlap": "The previous run was still open.", "misfire": "The run was missed and skipped by policy."}[reason])
                    if reason == "misfire":
                        audit(VERBS["misfire"], schedule.organization, None, schedule, f"Schedule {schedule.name} missed a run", late_seconds=int(late), policy=schedule.misfire_policy)
                result["skipped"] += 1
            else:
                jobs.enqueue(schedule.task_type, organization=schedule.organization, payload=schedule.payload, trigger=k.Trigger.SCHEDULE, schedule=schedule, idempotency_key=key,
                             scheduled_for=occurrence, max_attempts=schedule.max_attempts, now=now)
                result["created"] += 1
            nxt = recurrence.next_after(schedule.schedule_type, schedule.recurrence, recurrence.get_zone(schedule.timezone), schedule.start_at, schedule.end_at, now)
            schedule.last_run_at = now
            schedule.next_run_at = nxt
            if nxt is None:
                schedule.status = k.ScheduleStatus.FINISHED
                result["finished"] += 1
            schedule.save(update_fields=["last_run_at", "next_run_at", "status", "updated_at"])
    return result


# ----------------------------------------------------------------------------------------------------------------- recovery
def recover_expired(now: datetime | None = None, limit: int = 100) -> int:
    """Take back jobs whose lease expired (a runner died or hung).  A job that never started goes back to the queue; one that was running counts the attempt and retries or fails."""
    now = now or timezone.now()
    recovered = 0
    expired = list(ScheduledJob.objects.filter(status__in=[k.JobStatus.CLAIMED, k.JobStatus.RUNNING], lease_expires_at__lt=now).values_list("pk", flat=True)[: max(1, min(limit, k.MAX_BATCH))])
    for pk in expired:
        with transaction.atomic():
            job = ScheduledJob.objects.select_for_update(skip_locked=True).filter(pk=pk, status__in=[k.JobStatus.CLAIMED, k.JobStatus.RUNNING], lease_expires_at__lt=now).first()
            if job is None:
                continue
            task = registry.get(job.task_type)
            was_running = job.status == k.JobStatus.RUNNING
            fields: dict[str, Any] = {"lease_token": "", "lease_expires_at": None, "claimed_by": ""}
            if not was_running:
                ScheduledJob.objects.filter(pk=job.pk).update(status=k.JobStatus.PENDING, available_at=now, **fields)
            else:
                exhausted = job.attempt_count >= job.max_attempts or task is None
                decision = "retries exhausted" if exhausted else "retry scheduled"
                number = job.executions.count() + 1
                JobExecution.objects.create(job=job, number=number, started_at=job.started_at or now, finished_at=now, outcome=k.ExecutionOutcome.LEASE_EXPIRED, error_category="lease_expired",
                                            error_message="The runner stopped before the job finished.", retry_decision=decision, correlation_id=job.correlation_id, runner=job.claimed_by)
                if exhausted:
                    ScheduledJob.objects.filter(pk=job.pk).update(status=k.JobStatus.FAILED, finished_at=now, error_category="lease_expired", error_message="The runner stopped before the job finished.", **fields)
                else:
                    ScheduledJob.objects.filter(pk=job.pk).update(status=k.JobStatus.RETRY_PENDING, available_at=now + timezone.timedelta(seconds=backoff_seconds(task, job.attempt_count)),
                                                                   error_category="lease_expired", error_message="The runner stopped before the job finished.", **fields)
            audit(VERBS["lease_recovered"], job.organization, None, job, "Expired job lease recovered", task_type=job.task_type, was_running=was_running, correlation_id=job.correlation_id)
            recovered += 1
    return recovered


# ----------------------------------------------------------------------------------------------------------------- claim / begin / finish
def claim(limit: int = 10, *, runner: str = "", now: datetime | None = None, job_id: Any = None) -> list[tuple[Any, str]]:
    """Lease up to ``limit`` eligible jobs (or exactly ``job_id`` if eligible).  Returns ``[(pk, token)]``.  The transaction ends before anything runs."""
    now = now or timezone.now()
    lease = int(getattr(settings, "JOBS_LEASE_SECONDS", 600))
    out: list[tuple[Any, str]] = []
    with transaction.atomic():
        qs = ScheduledJob.objects.select_for_update(skip_locked=True).filter(status__in=[k.JobStatus.PENDING, k.JobStatus.RETRY_PENDING], available_at__lte=now)
        if job_id is not None:
            qs = qs.filter(pk=job_id)
        for job in qs.order_by("-priority", "available_at", "id")[: max(1, min(limit, k.MAX_BATCH))]:
            token = uuid.uuid4().hex
            job.status, job.lease_token, job.claimed_by = k.JobStatus.CLAIMED, token, runner or runner_id()
            job.lease_expires_at = now + timezone.timedelta(seconds=lease)
            job.save(update_fields=["status", "lease_token", "claimed_by", "lease_expires_at"])
            out.append((job.pk, token))
    return out


def release(pk: Any, token: str) -> bool:
    """Hand a claimed-but-not-started job back (a runner that is shutting down)."""
    return bool(ScheduledJob.objects.filter(pk=pk, status=k.JobStatus.CLAIMED, lease_token=token).update(status=k.JobStatus.PENDING, lease_token="", lease_expires_at=None, claimed_by=""))


def _begin(pk: Any, token: str, now: datetime) -> ScheduledJob | None:
    changed = ScheduledJob.objects.filter(pk=pk, status=k.JobStatus.CLAIMED, lease_token=token, lease_expires_at__gt=now).update(
        status=k.JobStatus.RUNNING, started_at=now, attempt_count=F("attempt_count") + 1)
    return ScheduledJob.objects.select_related("organization").get(pk=pk) if changed else None


def _finish(job: ScheduledJob, token: str, *, outcome: str, started: datetime, now: datetime, duration_ms: int, task: registry.Task | None, result: dict[str, Any] | None = None,
            category: str = "", message: str = "", retryable: bool = False, soft_exceeded: bool = False) -> str:
    """Record the attempt and move the job on - only if this runner still owns the lease."""
    message = sanitize(message)
    with transaction.atomic():
        owned = ScheduledJob.objects.select_for_update().filter(pk=job.pk, status=k.JobStatus.RUNNING, lease_token=token).first()
        if owned is None:
            return "lost"
        clear = {"lease_token": "", "lease_expires_at": None}
        if outcome == k.ExecutionOutcome.SUCCEEDED:
            status, decision, fields = k.JobStatus.SUCCEEDED, "", {"result": result or {}, "finished_at": now, "duration_ms": duration_ms, "error_category": "", "error_message": ""}
        elif retryable and task is not None and owned.attempt_count < owned.max_attempts:
            delay = backoff_seconds(task, owned.attempt_count)
            status, outcome = k.JobStatus.RETRY_PENDING, k.ExecutionOutcome.RETRYABLE
            decision = f"retry in {delay}s"
            fields = {"available_at": now + timezone.timedelta(seconds=delay), "error_category": category, "error_message": message, "duration_ms": duration_ms}
        else:
            exhausted = retryable
            status = k.JobStatus.FAILED
            outcome = k.ExecutionOutcome.EXHAUSTED if exhausted else k.ExecutionOutcome.PERMANENT
            decision = "retries exhausted" if exhausted else "permanent failure"
            fields = {"finished_at": now, "duration_ms": duration_ms, "error_category": category, "error_message": message}
        ScheduledJob.objects.filter(pk=job.pk).update(status=status, **fields, **clear)
        JobExecution.objects.create(job=owned, number=owned.executions.count() + 1, started_at=started, finished_at=now, outcome=outcome, duration_ms=duration_ms, error_category=category,
                                    error_message=message, retry_decision=decision, correlation_id=owned.correlation_id, runner=owned.claimed_by, soft_limit_exceeded=soft_exceeded)
        if status == k.JobStatus.RETRY_PENDING:
            audit(VERBS["retry_scheduled"], owned.organization, None, owned, "Job will be retried", category=category, attempt=owned.attempt_count, correlation_id=owned.correlation_id)
        elif status == k.JobStatus.FAILED:
            audit(VERBS["exhausted" if outcome == k.ExecutionOutcome.EXHAUSTED else "failed"], owned.organization, None, owned, "Job failed", category=category, attempt=owned.attempt_count,
                  task_type=owned.task_type, correlation_id=owned.correlation_id)
    return status


def execute(pk: Any, token: str, *, now: Callable[[], datetime] = timezone.now) -> str:
    """Run one claimed job.  Returns its final status, or ``"lost"`` when this runner no longer owns the lease (nothing was changed)."""
    started = now()
    job = _begin(pk, token, started)
    if job is None:
        return "lost"
    log_job("started", job, runner=job.claimed_by)
    task = registry.get(job.task_type)
    clock = time.monotonic()
    result: dict[str, Any] = {}
    outcome, retryable, category, message = k.ExecutionOutcome.SUCCEEDED, False, "", ""
    if task is None:
        outcome, category, message = k.ExecutionOutcome.PERMANENT, "unknown_task", "That task type is not available."
    else:
        try:
            ctx = registry.Context(job_id=str(job.pk), organization=job.organization, payload=registry.clean_payload(task, job.payload), correlation_id=job.correlation_id)
            raw = task.handler(ctx)
            result = {str(key): value for key, value in (raw or {}).items() if isinstance(value, (int, float, str, bool)) or value is None}
        except RetryableError as exc:
            outcome, retryable, category, message = k.ExecutionOutcome.RETRYABLE, True, "temporary_failure", str(exc) or "A temporary failure occurred."
        except (PermanentError, JobError) as exc:
            outcome, category, message = k.ExecutionOutcome.PERMANENT, "permanent_failure", str(exc) or "The job cannot be completed."
        except (OperationalError, OSError, TimeoutError) as exc:  # infrastructure: worth another attempt; the text is never stored
            outcome, retryable, category, message = k.ExecutionOutcome.RETRYABLE, True, "infrastructure", f"A temporary {type(exc).__name__} occurred."
        except Exception as exc:  # noqa: BLE001 - a defect: permanent, and only the class name is recorded (no traceback, no values)
            outcome, category, message = k.ExecutionOutcome.PERMANENT, "unexpected_error", f"Unexpected {type(exc).__name__}."
    finished = now()
    duration_ms = int((time.monotonic() - clock) * 1000)
    soft = bool(task and duration_ms > task.soft_limit_seconds * 1000)
    status = _finish(job, token, outcome=outcome, started=started, now=finished, duration_ms=duration_ms, task=task, result=result, category=category, message=message, retryable=retryable,
                     soft_exceeded=soft)
    job.refresh_from_db()
    log_job("finished", job, duration_ms=duration_ms, status=status, category=category)
    return status


# ----------------------------------------------------------------------------------------------------------------- the loop body
def heartbeat(runner: str, *, summary: dict[str, Any] | None = None, processed: int = 0, started: datetime | None = None, now: datetime | None = None) -> None:
    now = now or timezone.now()
    row, _ = RunnerHeartbeat.objects.get_or_create(runner=runner, defaults={"last_seen_at": now})
    RunnerHeartbeat.objects.filter(pk=runner).update(last_seen_at=now, jobs_processed=F("jobs_processed") + processed, **({"last_started_at": started} if started else {}),
                                                    **({"last_summary": summary} if summary is not None else {}))


def run_once(*, limit: int = 20, max_seconds: int = 240, runner: str = "", stop: Callable[[], bool] = lambda: False, now: Callable[[], datetime] = timezone.now) -> dict[str, Any]:
    """One bounded pass: recover expired leases, materialize due schedules, then claim and run jobs until ``limit`` jobs, ``max_seconds`` or ``stop()``."""
    runner = runner or runner_id()
    limit = max(1, min(int(limit), k.MAX_BATCH))
    deadline = time.monotonic() + max(1, min(int(max_seconds), k.MAX_RUNTIME_SECONDS))
    started = now()
    summary: dict[str, Any] = {"runner": runner, "recovered": recover_expired(started), "materialized": materialize_due(started), "ran": 0, "succeeded": 0, "retry": 0, "failed": 0, "lost": 0}
    heartbeat(runner, started=started, now=started)
    claimed = claim(limit, runner=runner, now=now())
    for index, (pk, token) in enumerate(claimed):
        if stop() or time.monotonic() >= deadline:
            for rest_pk, rest_token in claimed[index:]:
                release(rest_pk, rest_token)
            summary["released"] = len(claimed) - index
            break
        status = execute(pk, token, now=now)
        summary["ran"] += 1
        key = {k.JobStatus.SUCCEEDED: "succeeded", k.JobStatus.RETRY_PENDING: "retry", k.JobStatus.FAILED: "failed", "lost": "lost"}.get(status)
        if key:
            summary[key] += 1
    heartbeat(runner, summary={key: value for key, value in summary.items() if isinstance(value, (int, str))}, processed=summary["ran"], now=now())
    return summary
