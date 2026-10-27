"""A read-only health summary of the notification pipeline: backlog, failures and the oldest unprocessed item (exit code 1 when something needs attention)."""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Count, Min

from apps.notifications import constants as k
from apps.notifications.models import NotificationDelivery, NotificationEvent


class Command(BaseCommand):
    help = "Show notification backlog and failures."

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        events = dict(NotificationEvent.objects.values_list("status").annotate(n=Count("pk")))
        deliveries = dict(NotificationDelivery.objects.values_list("status").annotate(n=Count("pk")))
        oldest = NotificationEvent.objects.filter(status=k.EventStatus.PENDING).aggregate(o=Min("created_at"))["o"]
        self.stdout.write("Events: " + (", ".join(f"{s}={n}" for s, n in sorted(events.items())) or "none"))
        self.stdout.write("Deliveries: " + (", ".join(f"{s}={n}" for s, n in sorted(deliveries.items())) or "none"))
        self.stdout.write(f"Oldest pending event: {oldest.isoformat() if oldest else 'none'}")
        attention = events.get(k.EventStatus.FAILED, 0) + deliveries.get(k.DeliveryStatus.FAILED, 0)
        if attention:
            self.stderr.write(self.style.WARNING(f"{attention} item(s) need attention (failed events or deliveries)."))
            raise SystemExit(1)
