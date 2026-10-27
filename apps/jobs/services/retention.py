"""Retention of scheduling history: finished jobs (and, by cascade, their execution history) older than ``JOBS_HISTORY_RETENTION_DAYS``; stale runner heartbeats."""

from __future__ import annotations

from datetime import datetime

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .. import constants as k
from ..errors import JobError
from ..models import RunnerHeartbeat, ScheduledJob
from .base import VERBS, audit


def cleanup(*, days: int | None = None, limit: int = 1000, now: datetime | None = None) -> dict[str, int]:
    """Delete up to ``limit`` finished jobs older than ``days`` (default from settings, at least 7).  Open jobs are never touched."""
    days = int(days if days is not None else getattr(settings, "JOBS_HISTORY_RETENTION_DAYS", 90))
    if days < 7:
        raise JobError("History is kept for at least 7 days.", "retention_too_short")
    now = now or timezone.now()
    cutoff = now - timezone.timedelta(days=days)
    ids = list(ScheduledJob.objects.filter(status__in=list(k.TERMINAL)).filter(Q(finished_at__lt=cutoff) | Q(finished_at__isnull=True, created_at__lt=cutoff)).values_list("pk", flat=True)[: max(1, min(limit, 10000))])
    deleted = ScheduledJob.objects.filter(pk__in=ids).delete()[0] if ids else 0
    beats = RunnerHeartbeat.objects.filter(last_seen_at__lt=now - timezone.timedelta(days=30)).delete()[0]
    if deleted or beats:
        audit(VERBS["purged"], None, None, None, f"Scheduling history cleanup removed {deleted} row(s)", rows=deleted, heartbeats=beats, days=days)
    return {"rows_deleted": deleted, "heartbeats_deleted": beats}
