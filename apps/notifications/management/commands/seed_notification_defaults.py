"""Create the built-in default notification rules for every organization (idempotent; existing rules are never overwritten)."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.notifications import services
from apps.organizations.models import Organization


class Command(BaseCommand):
    help = "Seed the default notification rules (idempotent)."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("--organization", help="Organization slug (default: all).")

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        orgs = Organization.objects.all()
        if options["organization"]:
            orgs = orgs.filter(slug=options["organization"])
        created = sum(services.seed_defaults(org) for org in orgs)
        self.stdout.write(self.style.SUCCESS(f"Created {created} default notification rule(s)."))
