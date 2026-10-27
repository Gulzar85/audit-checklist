"""Run one specific job now (a registered task type) - for operators; the job goes through the same lease / history / retry rules."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.jobs import services
from apps.jobs.models import ScheduledJob


class Command(BaseCommand):
    help = "Claim and execute one job by id (only if it is due and open)."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("job_id")

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        try:
            job = ScheduledJob.objects.get(pk=options["job_id"])
        except (ScheduledJob.DoesNotExist, ValueError, Exception) as exc:  # noqa: BLE001
            raise CommandError("No such job.") from exc
        claimed = services.claim(1, job_id=job.pk)
        if not claimed:
            raise CommandError(f"The job is {job.status} and cannot be run now (it must be pending or waiting to retry, and due).")
        status = services.execute(*claimed[0])
        self.stdout.write(f"Job finished as: {status}")
