"""Scheduling constants (Phase 18): statuses, the transition table and the hard limits every service enforces."""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class JobStatus(models.TextChoices):
    PENDING = "pending", _("Pending")  # waiting for its time
    CLAIMED = "claimed", _("Claimed")  # a runner holds the lease, the handler has not started
    RUNNING = "running", _("Running")  # the handler is executing (outside any database transaction)
    SUCCEEDED = "succeeded", _("Succeeded")  # the handler finished according to its contract (NOT "the email arrived")
    RETRY_PENDING = "retry_pending", _("Waiting to retry")
    FAILED = "failed", _("Failed")  # permanent failure or retries exhausted
    CANCELLED = "cancelled", _("Cancelled")  # cancelled before it started
    SKIPPED = "skipped", _("Skipped")  # not run on purpose (e.g. the schedule was disabled, or a missed run was skipped)


TERMINAL = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.SKIPPED})
OPEN = frozenset({JobStatus.PENDING, JobStatus.CLAIMED, JobStatus.RUNNING, JobStatus.RETRY_PENDING})
#: A job may be cancelled until the handler has started: a running job always finishes (handlers are not killed; they are idempotent).
CANCELLABLE = frozenset({JobStatus.PENDING, JobStatus.RETRY_PENDING, JobStatus.CLAIMED})

TRANSITIONS: dict[str, frozenset[str]] = {
    JobStatus.PENDING: frozenset({JobStatus.CLAIMED, JobStatus.CANCELLED, JobStatus.SKIPPED}),
    JobStatus.RETRY_PENDING: frozenset({JobStatus.CLAIMED, JobStatus.CANCELLED, JobStatus.SKIPPED}),
    JobStatus.CLAIMED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.RETRY_PENDING, JobStatus.FAILED}),  # the last two: the lease expired
    JobStatus.RUNNING: frozenset({JobStatus.SUCCEEDED, JobStatus.RETRY_PENDING, JobStatus.FAILED}),
    JobStatus.FAILED: frozenset({JobStatus.PENDING}),  # manual retry
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
    JobStatus.SKIPPED: frozenset(),
}


class ScheduleType(models.TextChoices):
    ONE_TIME = "one_time", _("One time")
    INTERVAL = "interval", _("Every N minutes")
    DAILY = "daily", _("Daily")
    WEEKLY = "weekly", _("Weekly")
    MONTHLY = "monthly", _("Monthly")


class ScheduleStatus(models.TextChoices):
    ACTIVE = "active", _("Active")
    PAUSED = "paused", _("Paused")  # temporarily stopped; resuming never back-fills the paused period
    DISABLED = "disabled", _("Disabled")
    FINISHED = "finished", _("Finished")  # a one-time schedule that ran, or a recurring one past its end


class MisfirePolicy(models.TextChoices):
    SKIP = "skip", _("Skip missed runs")
    RUN_ONCE = "run_once", _("Run once to catch up")


class ExecutionOutcome(models.TextChoices):
    SUCCEEDED = "succeeded", _("Succeeded")
    RETRYABLE = "retryable_failure", _("Failed, will retry")
    PERMANENT = "permanent_failure", _("Failed permanently")
    EXHAUSTED = "retries_exhausted", _("Retries exhausted")
    LEASE_EXPIRED = "lease_expired", _("Lease expired")


class Trigger(models.TextChoices):
    SCHEDULE = "schedule", _("Schedule")
    MANUAL = "manual", _("Run now")
    API = "api", _("API")
    SYSTEM = "system", _("System")


# --- limits (server-controlled; nothing here is configurable by a client) -------------------------------------------------------------
MAX_ATTEMPTS_LIMIT = 10
MAX_RETRY_DELAY_SECONDS = 6 * 3600
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 7 * 24 * 60
MAX_DELAY_SECONDS = 365 * 24 * 3600
MAX_PAYLOAD_BYTES = 4096
MAX_BATCH = 200
MAX_RUNTIME_SECONDS = 3300  # a single runner invocation never runs longer than this
HEARTBEAT_STALE_SECONDS = 15 * 60
ERROR_LIMIT = 200
