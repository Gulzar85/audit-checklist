"""Retry the security check of files that are still pending (scanner was down / not yet configured).

Run it from a scheduler (cron / systemd timer / Azure scheduler): ``python manage.py scan_evidence --limit 200``.  Bounded, idempotent,
and a file is never made available unless the scanner answers clean (or the explicit development policy says no scan is required).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.evidence import lifecycle
from apps.evidence.constants import AssetStatus
from apps.evidence.models import EvidenceAsset


class Command(BaseCommand):
    help = "Re-run the malware scan for files in pending_scan."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("--limit", type=int, default=200)

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        pending = EvidenceAsset.objects.filter(status=AssetStatus.PENDING_SCAN).order_by("created_at")[: options["limit"]]
        counts: dict[str, int] = {}
        for asset in pending:
            result = lifecycle.scan_asset(asset)
            counts[result.status] = counts.get(result.status, 0) + 1
        summary = ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "nothing pending"
        self.stdout.write(self.style.SUCCESS(f"Scanned: {summary}."))
