"""Delete old offline synchronization receipts (idempotency records).

A receipt only needs to outlive the longest time a device can sit on an unsent operation and retry it.  The default (90 days) is far
beyond the offline retention window; after pruning, a (very late) retry of the same operation id would be treated as a new operation
and still be checked against the current revision, so an old retry can at worst produce a conflict - never a silent double write.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.executions.models import SyncReceipt


class Command(BaseCommand):
    help = "Delete synchronization receipts older than --days (default 90)."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("--days", type=int, default=90)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        cutoff = timezone.now() - timedelta(days=options["days"])
        old = SyncReceipt.objects.filter(created_at__lt=cutoff)
        count = old.count()
        if options["dry_run"]:
            self.stdout.write(f"{count} receipt(s) older than {options['days']} days would be deleted.")
            return
        old.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {count} receipt(s) older than {options['days']} days."))
