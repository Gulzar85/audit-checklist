"""Delete notification history older than the retention period (``NOTIFICATION_RETENTION_DAYS``).  Only fully finished rows are removed; the audit log is kept."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.notifications import services


class Command(BaseCommand):
    help = "Purge old, finished notification events / notifications / deliveries (bounded)."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("--days", type=int, default=None, help="Keep this many days (default NOTIFICATION_RETENTION_DAYS).")
        parser.add_argument("--limit", type=int, default=1000)

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        try:
            result = services.purge(days=options["days"], limit=max(1, min(options["limit"], 10000)))
        except services.NotificationError as exc:
            raise CommandError(exc.message) from exc
        self.stdout.write(self.style.SUCCESS("Purged: " + ", ".join(f"{k}={v}" for k, v in sorted(result.items()))))

