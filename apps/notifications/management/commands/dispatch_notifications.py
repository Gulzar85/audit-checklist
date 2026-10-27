"""Send the due email deliveries (first attempts and retries).  The Phase 18 scheduler runs this; it can also be run by hand."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.notifications import services


class Command(BaseCommand):
    help = "Dispatch pending and retry-due notification deliveries (bounded, concurrency-safe)."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("--limit", type=int, default=None, help="Maximum deliveries in this run (default: NOTIFICATION_BATCH_LIMIT).")

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        limit = options["limit"]
        if limit is not None and not 1 <= limit <= 1000:
            raise CommandError("--limit must be between 1 and 1000.")
        result = services.dispatch_batch(limit)
        self.stdout.write(self.style.SUCCESS("Dispatched notification deliveries: " + ", ".join(f"{k}={v}" for k, v in sorted(result.items()))))
