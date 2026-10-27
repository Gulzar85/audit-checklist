"""Re-queue failed jobs (bounded, optionally for one task type or organization).  Operator command: the system actor, a reason is recorded."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.jobs import constants as k
from apps.jobs import registry
from apps.jobs.models import ScheduledJob
from apps.jobs.services.base import VERBS, audit


class Command(BaseCommand):
    help = "Retry failed jobs (at most --limit)."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("--task", default="", help="Only this task type.")
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--reason", default="Retried from the command line")

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        if options["task"] and registry.get(options["task"]) is None:
            raise CommandError("Unknown task type.")
        limit = max(1, min(options["limit"], k.MAX_BATCH))
        qs = ScheduledJob.objects.filter(status=k.JobStatus.FAILED, task_type__in=list(registry.REGISTRY))
        if options["task"]:
            qs = qs.filter(task_type=options["task"])
        count = 0
        now = timezone.now()
        for job in qs.order_by("finished_at")[:limit]:
            changed = ScheduledJob.objects.filter(pk=job.pk, status=k.JobStatus.FAILED).update(status=k.JobStatus.PENDING, attempt_count=0, available_at=now, finished_at=None, started_at=None,
                                                                                               duration_ms=None, error_category="", error_message="")
            if changed:
                count += 1
                audit(VERBS["manual_retry"], job.organization, None, job, "Job retried from the command line", reason=options["reason"][:200], task_type=job.task_type, correlation_id=job.correlation_id)
        self.stdout.write(self.style.SUCCESS(f"Re-queued {count} failed job(s)."))
