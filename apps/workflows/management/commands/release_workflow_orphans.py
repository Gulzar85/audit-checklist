"""Release workflow tasks whose assignee is no longer active or eligible (the documented orphan policy).

A task of a role / permission policy returns to the claimable pool; a task named for one person is released and its workflow flagged for administrative attention.
Nothing is deleted.  Safe to run repeatedly (e.g. from a scheduler); the engine does not depend on it for correctness.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.workflows import services


class Command(BaseCommand):
    help = "Release open workflow tasks whose assignee is inactive or no longer eligible."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("--organization", help="Organization slug.")

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        organization = None
        if options["organization"]:
            from apps.organizations.models import Organization

            organization = Organization.objects.filter(slug=options["organization"]).first()
        released = services.release_orphaned_tasks(organization=organization)
        self.stdout.write(self.style.SUCCESS(f"Released {released} task(s)."))
