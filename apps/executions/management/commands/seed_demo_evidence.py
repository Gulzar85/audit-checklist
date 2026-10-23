"""Seed a checklist with photo, file and signature questions and a fresh assignment for the evidence walkthrough.

Idempotent, development data only. Builds on ``seed_demo_executions`` (organization, locations, actor). The checklist has a required
photo, an optional document, an optional signature and a temperature rule that requires a photo when the reading is above 8.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.assignments import services as assignment_services
from apps.assignments.models import ChecklistAssignment
from apps.checklists.models import ChecklistVersion
from apps.checklists.seed import load_template
from apps.executions.management.commands.seed_demo_executions import Command as ExecutionsDemo
from apps.locations.models import Location
from apps.organizations.models import Organization

CODE = "DEMO-EVD"

DEFINITION = {
    "code": CODE, "name": "Demo Evidence Checklist", "category": "Safety",
    "sections": [
        {"code": "S1", "name": "Evidence", "items": [
            {"code": "OK", "question": "Is everything in order?", "answer_type": "yes_no"},
            {"code": "PHOTO", "question": "Photo of the area", "answer_type": "photo", "required": True,
             "config": {"max_files": 2, "max_size_mb": 5, "allowed_extensions": ["jpg", "jpeg", "png", "webp"]}},
            {"code": "DOC", "question": "Supporting document", "answer_type": "file", "required": False,
             "config": {"max_files": 2, "max_size_mb": 5, "allowed_extensions": ["pdf", "jpg", "png"]}},
            {"code": "SIGN", "question": "Manager signature", "answer_type": "signature", "required": False},
            {"code": "TEMP", "question": "Fridge temperature", "answer_type": "numeric", "required": False,
             "config": {"min": 0, "max": 10, "decimals": 1}},
        ]},
    ],
    "rules": [
        {"name": "A high temperature needs a photo",
         "condition": {"source_item_code": "TEMP", "comparison": "GREATER_THAN", "value": "8"},
         "actions": [{"type": "REQUIRE_EVIDENCE", "target": {"type": "item", "code": "TEMP"}, "kinds": ["photo"], "min_count": 1}]},
    ],
}


class Command(BaseCommand):
    help = "Seed an evidence checklist and a not-yet-started assignment (photo / file / signature questions)."

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
        if version is None:
            template, _ = load_template(organization, DEFINITION)
            version = template.versions.get(version_number=1)
        location = Location.objects.filter(organization=organization, code="DEMO-R-3").first()
        if location is None:
            raise CommandError("The demo location is missing; run seed_demo_executions first.")
        existing = ChecklistAssignment.objects.filter(version=version, location=location, status__in=["assigned", "in_progress"]).first()
        if existing is None:
            existing = assignment_services.create_assignment(
                actor=ExecutionsDemo()._actor(organization), version=version, location=location, issue=True, priority="high",
                due_at=timezone.now() + timedelta(days=2), instructions="Evidence walkthrough.")
        self.stdout.write(f"{existing.reference}: open for {location.name}.")
        self.stdout.write("Walkthrough: open /executions/ -> start it -> add a photo to 'Photo of the area' (docs/evidence-architecture.md).")
        self.stdout.write(self.style.SUCCESS("Evidence demo ready."))
