"""Convert pending outbox events into notifications (and deliveries).  The Phase 18 scheduler's first entry point; safe to run concurrently and repeatedly."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.notifications import services


class Command(BaseCommand):
    help = "Process pending notification events (bounded, idempotent)."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("--limit", type=int, default=50, help="Maximum events in this run (1-1000).")
        parser.add_argument("--no-dispatch", action="store_true", help="Create notifications and queue deliveries but do not send email now.")

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        limit = options["limit"]
        if not 1 <= limit <= 1000:
            raise CommandError("--limit must be between 1 and 1000.")
        result = services.process_pending(limit, dispatch=not options["no_dispatch"])
        self.stdout.write(self.style.SUCCESS("Processed notification events: " + ", ".join(f"{k}={v}" for k, v in sorted(result.items()))))
        if result.get("failed"):
            raise CommandError(f"{result['failed']} event(s) failed (see the activity log).")
