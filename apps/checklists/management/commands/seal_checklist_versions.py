"""Record the integrity hash of published/superseded versions that have none (published before hashes existed).

Idempotent.  Sealing writes to a non-draft version, so it runs inside the explicit maintenance override
(``guards.allow_definition_changes``) and every sealed version is recorded in the activity log.  It computes the hash of the
rows *as they are now*: run ``verify_checklist_integrity`` afterwards (and be aware that a legacy version altered before this
command ran can no longer be told apart from its original).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.checklists import constants as c
from apps.checklists import lifecycle
from apps.checklists.models import ChecklistVersion


class Command(BaseCommand):
    help = "Seal published/superseded checklist versions that have no integrity hash yet."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("--organization", help="Only this organization (slug).")

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        versions = ChecklistVersion.objects.exclude(status=c.VersionStatus.DRAFT).filter(integrity_hash="").select_related("template__organization")
        if options["organization"]:
            versions = versions.filter(template__organization__slug=options["organization"])
        sealed = sum(1 for version in versions.order_by("template__code", "version_number") if lifecycle.seal_version(version))
        self.stdout.write(self.style.SUCCESS(f"Sealed {sealed} version(s)."))
