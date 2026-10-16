"""Load the two audit checklists (from the source workbooks) into an organization.  Idempotent."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.checklists import seed
from apps.organizations.models import Organization

FILES = {
    "spot-visit": "spot_visit.json",
    "rfsa": "rfsa.json",
}


class Command(BaseCommand):
    help = (
        "Create the 'Restaurant Food Safety Spot Visit' and 'Restaurant Food Safety Audit (RFSA)' checklist templates "
        "for one organization. Safe to run repeatedly (existing templates are left alone)."
    )

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("--organization", help="Organization slug (required when there is more than one).")
        parser.add_argument("--only", choices=sorted(FILES), help="Load just one of the two checklists.")
        parser.add_argument("--draft", action="store_true", help="Leave version 1 as an editable draft (do not publish).")

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        organizations = Organization.objects.all()
        if options["organization"]:
            organizations = organizations.filter(slug=options["organization"])
        if organizations.count() != 1:
            raise CommandError("Name exactly one organization with --organization <slug>.")
        organization = organizations.get()
        for key, filename in FILES.items():
            if options["only"] and options["only"] != key:
                continue
            template, created = seed.load_template(organization, seed.load_data_file(filename), publish=not options["draft"])
            items = sum(s.items.count() for s in template.versions.first().sections.all())
            self.stdout.write(self.style.SUCCESS(
                f"{'Created' if created else 'Already present'}: {template.code} {template.name} ({items} items)"
            ))