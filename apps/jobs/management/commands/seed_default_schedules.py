"""Create the built-in platform-wide maintenance schedules (idempotent; an existing schedule - even a disabled or edited one - is never overwritten)."""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.jobs import constants as k
from apps.jobs import recurrence
from apps.jobs.models import ScheduleDefinition

DEFAULTS = (
    ("Process pending notifications", "notifications.process_pending", k.ScheduleType.INTERVAL, {"every_minutes": 5}, {}),
    ("Corrective action reminders", "corrective_actions.generate_due_reminders", k.ScheduleType.DAILY, {"time": "07:00"}, {}),
    ("Purge old notification history", "notifications.purge_history", k.ScheduleType.DAILY, {"time": "02:30"}, {}),
    ("Clean up scheduling history", "jobs.cleanup_old_execution_history", k.ScheduleType.DAILY, {"time": "03:00"}, {}),
)


class Command(BaseCommand):
    help = "Seed the default platform-wide schedules."

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        from apps.jobs import registry

        now = timezone.now()
        created = 0
        for name, task, stype, rec, payload in DEFAULTS:
            if ScheduleDefinition.objects.filter(organization__isnull=True, name=name).exists():
                continue
            zone = recurrence.get_zone("UTC")
            clean = registry.clean_payload(registry.require(task), payload)
            ScheduleDefinition.objects.create(name=name, task_type=task, schedule_type=stype, timezone="UTC", start_at=now, recurrence=recurrence.clean_recurrence(stype, rec), payload=clean,
                                              next_run_at=recurrence.next_after(stype, rec, zone, now, None, now), max_attempts=registry.require(task).max_attempts)
            created += 1
        self.stdout.write(self.style.SUCCESS(f"Created {created} default schedule(s)."))
