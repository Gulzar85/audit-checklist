"""Publish due-soon / overdue events for open corrective actions (idempotent per action and due date).  Phase 18 will schedule this; Phase 17 only provides it."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.notifications import services


class Command(BaseCommand):
    help = "Emit corrective-action reminder events (nothing is sent here; rules, recipients and delivery run as for every event)."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("--within-hours", type=int, default=48, help="Due-soon horizon in hours (1-720).")
        parser.add_argument("--limit", type=int, default=500)

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        if not 1 <= options["within_hours"] <= 720:
            raise CommandError("--within-hours must be between 1 and 720.")
        result = services.emit_due_reminders(within_hours=options["within_hours"], limit=max(1, min(options["limit"], 5000)))
        self.stdout.write(self.style.SUCCESS(f"New reminder events: due_soon={result['due_soon']}, overdue={result['overdue']}."))
