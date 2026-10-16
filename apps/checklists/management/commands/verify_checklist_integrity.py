"""Verify every published/superseded version against the integrity hash sealed at publication (read-only).

Exit status 1 when any version no longer matches (or, with ``--strict``, is not sealed), so it can run in a scheduled check.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.checklists import constants as c
from apps.checklists.manifest import verify_version_integrity
from apps.checklists.models import ChecklistVersion


class Command(BaseCommand):
    help = "Verify the integrity hashes of published and superseded checklist versions."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("--organization", help="Only this organization (slug).")
        parser.add_argument("--strict", action="store_true", help="Also fail for versions that were never sealed.")

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        versions = ChecklistVersion.objects.exclude(status=c.VersionStatus.DRAFT).select_related("template")
        if options["organization"]:
            versions = versions.filter(template__organization__slug=options["organization"])
        problems = checked = 0
        for version in versions.order_by("template__code", "version_number"):
            result = verify_version_integrity(version)
            checked += 1
            if result.status == "mismatch" or (options["strict"] and result.status != "verified"):
                problems += 1
                self.stderr.write(f"{version}: {result.label}")
        if problems:
            raise CommandError(f"{problems} of {checked} version(s) failed verification.")
        self.stdout.write(self.style.SUCCESS(f"{checked} version(s) verified."))
