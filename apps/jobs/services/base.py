"""Shared plumbing: audit and structured logging (job id, task, organization, attempt, correlation id, duration - never payload values, tokens or tracebacks)."""

from __future__ import annotations

import logging
from typing import Any

from apps.core.services import log_activity

logger = logging.getLogger("apps.jobs")

VERBS = {
    "schedule_created": "jobs.schedule_created", "schedule_updated": "jobs.schedule_updated", "schedule_enabled": "jobs.schedule_enabled", "schedule_disabled": "jobs.schedule_disabled",
    "schedule_paused": "jobs.schedule_paused", "schedule_resumed": "jobs.schedule_resumed", "manual_run": "jobs.manual_run", "cancelled": "jobs.cancelled", "manual_retry": "jobs.manual_retry",
    "failed": "jobs.failed", "retry_scheduled": "jobs.retry_scheduled", "exhausted": "jobs.retries_exhausted", "lease_recovered": "jobs.lease_recovered", "misfire": "jobs.misfire",
    "purged": "jobs.history_purged",
}


def audit(verb: str, organization: Any, actor: Any, target: Any, description: str, **metadata: Any) -> None:
    log_activity(verb=verb, organization=organization, actor=actor if getattr(actor, "is_authenticated", False) else None, target=target, description=description[:400], metadata=metadata)


def log_job(event: str, job: Any, **fields: Any) -> None:
    logger.info("jobs.%s", event, extra={"job_id": str(job.pk), "task_type": job.task_type, "organization_id": str(job.organization_id or ""), "attempt": job.attempt_count,
                                         "correlation_id": job.correlation_id, **{key: value for key, value in fields.items() if key in {"duration_ms", "status", "runner", "category"}}})
