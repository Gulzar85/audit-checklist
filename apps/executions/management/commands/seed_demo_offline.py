"""Prepare the demo for the offline walkthrough (idempotent, development data only).

Builds on ``seed_demo_executions`` (a published Daily Safety Checklist - every question type and rule in it is supported offline) and
makes sure there is a fresh assignment, at a Restaurant location, that has NOT been started: ideal for "Prepare for offline use".
Prints the manual verification steps (docs/offline-execution.md has the full script).
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.assignments import services as assignment_services
from apps.assignments.models import ChecklistAssignment
from apps.checklists.models import ChecklistVersion
from apps.executions.management.commands.seed_demo_executions import CODE, Command as ExecutionsDemo
from apps.locations.models import Location
from apps.organizations.models import Organization


class Command(BaseCommand):
    help = "Seed a not-yet-started, offline-capable assignment for the offline walkthrough."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("--organization", help="Organization slug (default: the only organization).")

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        organizations = Organization.objects.all()
        if options["organization"]:
            organizations = organizations.filter(slug=options["organization"])
        if organizations.count() != 1:
            raise CommandError("Name exactly one organization with --organization <slug>.")
        organization = organizations.get()
        call_command("seed_demo_executions", organization=organization.slug, stdout=self.stdout)
        version = ChecklistVersion.objects.filter(template__organization=organization, template__code=CODE, status="published").first()
        location = Location.objects.filter(organization=organization, code="DEMO-R-3").first()
        if version is None or location is None:
            raise CommandError("The demo checklist or location is missing; run seed_demo_executions first.")
        existing = ChecklistAssignment.objects.filter(version=version, location=location, status__in=["assigned", "in_progress"]).first()
        if existing is not None:
            self.stdout.write(f"{existing.reference}: already open for {location.name} - use it for the walkthrough.")
        else:
            actor = ExecutionsDemo()._actor(organization)
            existing = assignment_services.create_assignment(actor=actor, version=version, location=location, issue=True, priority="high",
                                                             due_at=timezone.now() + timedelta(days=2), instructions="Offline walkthrough.")
            self.stdout.write(f"{existing.reference}: issued for {location.name}.")
        self.stdout.write("Walkthrough: open /executions/ -> Offline, prepare it, go offline, answer, reload, reconnect, Sync now.")
        self.stdout.write(self.style.SUCCESS("Offline demo ready."))
