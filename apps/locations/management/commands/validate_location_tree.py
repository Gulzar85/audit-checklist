"""Audit the location hierarchy for integrity problems.

    python manage.py validate_location_tree
    python manage.py validate_location_tree --organization ACME

Exits non-zero when anything is wrong, so it can gate a deploy, a restore or a
nightly check.  Normal writes cannot produce these states (constraints, triggers
and the service layer prevent them); a finding means data arrived another way.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.locations.selectors import find_integrity_problems
from apps.organizations.models import Organization, normalize_organization_code


class Command(BaseCommand):
    help = "Report hierarchy integrity problems (cycles, cross-organization links, inactive parents)."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument(
            "--organization",
            metavar="CODE",
            help="Only audit the organization with this code.",
        )

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        organization = None
        if options["organization"]:
            code = normalize_organization_code(options["organization"])
            try:
                organization = Organization.objects.get(code=code)
            except Organization.DoesNotExist:
                raise CommandError(f"No organization with code {code!r}.") from None

        problems = find_integrity_problems(organization)
        scope = f"organization {organization.code}" if organization else "all organizations"
        if not problems:
            self.stdout.write(self.style.SUCCESS(f"Location tree is consistent ({scope})."))
            return

        for problem in problems:
            self.stderr.write(f"[{problem.code}] {problem.location_id}: {problem.message}")
        raise CommandError(f"{len(problems)} integrity problem(s) found in {scope}.")
