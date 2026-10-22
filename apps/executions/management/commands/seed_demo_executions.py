"""Demonstrate checklist execution end to end on generic data (idempotent, development data only).

Publishes a *Daily Safety Checklist* (boolean, yes/no/N-A, single choice, numeric, text and date questions plus a conditional
rule: "No" to the fire-exit question reveals and requires a follow-up), assigns it to a Restaurant and to a Warehouse - the same
generic engine, different location types - and drives the real services: an in-progress execution with saved answers (and one
question refused by validation, as in real use), a submitted read-only execution, and an assignment still waiting to be started.
Nothing here is specific to one organization or location type.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.assignments import services as assignment_services
from apps.assignments.management.commands.seed_demo_assignments import Command as AssignmentsDemo
from apps.checklists.models import ChecklistItem
from apps.checklists.seed import load_template
from apps.executions import services
from apps.executions.models import ChecklistExecution
from apps.organizations.models import Organization
from apps.permissions.authorization import has_permission
from apps.permissions.registry import P

CODE = "DEMO-EXE-DSC"

CHECKLIST = {
    "code": CODE, "name": "Daily Safety Checklist", "category": "Safety", "minutes": 10,
    "description": "A generic daily safety checklist used to demonstrate execution.",
    "instructions": "Walk the site and answer every question. Describe any issue you find.",
    "sections": [
        {"code": "FIRE", "name": "Fire Safety", "items": [
            {"code": "FIRE-EXIT", "question": "Is the fire exit accessible?", "answer_type": "boolean"},
            {"code": "EXTINGUISHER", "question": "Is the fire extinguisher in date?", "answer_type": "yes_no_na"},
        ]},
        {"code": "STORAGE", "name": "Storage", "items": [
            {"code": "TEMP", "question": "Record the cold-room temperature.", "answer_type": "numeric", "config": {"min": -5, "max": 10, "decimals": 1, "unit": "°C"}},
            {"code": "TIDY", "question": "How tidy is the storage area?", "answer_type": "single_choice",
             "options": [["good", "Good"], ["fair", "Fair"], ["poor", "Poor"]]},
            {"code": "CHECKED", "question": "Date of the last stock check", "answer_type": "date", "required": False},
        ]},
        {"code": "GENERAL", "name": "General", "items": [
            {"code": "ISSUE", "question": "Describe the issue.", "answer_type": "long_text", "required": False},
            {"code": "NOTES", "question": "Anything else to report?", "answer_type": "text", "required": False},
        ]},
    ],
    "rules": [
        {"name": "A blocked exit needs a description", "condition": {"source_item_code": "FIRE-EXIT", "comparison": "EQUALS", "value": False},
         "actions": [{"type": "SHOW_ITEM", "target": {"type": "item", "code": "ISSUE"}}, {"type": "REQUIRE_ITEM", "target": {"type": "item", "code": "ISSUE"}}]},
    ],
}


class Command(BaseCommand):
    help = "Seed the execution demo (checklist v1, an in-progress and a submitted execution) for one organization."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("--organization", help="Organization slug (default: the only organization).")

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        organizations = Organization.objects.all()
        if options["organization"]:
            organizations = organizations.filter(slug=options["organization"])
        if organizations.count() != 1:
            raise CommandError("Name exactly one organization with --organization <slug>.")
        organization = organizations.get()
        if ChecklistExecution.objects.filter(organization=organization, version__template__code=CODE).exists():
            self.stdout.write(f"Execution demo {CODE}: already present.")
            return
        actor = self._actor(organization)
        locations = AssignmentsDemo()._hierarchy(organization)
        template, _created = load_template(organization, CHECKLIST)
        version = template.versions.get(version_number=1)
        self.stdout.write(f"Checklist {template.name} version {version.version_number} is published.")

        def assign(code: str, **kw):  # noqa: ANN202
            return assignment_services.create_assignment(actor=actor, version=version, location=locations[code], issue=True,
                                                         due_at=timezone.now() + timedelta(days=1), **kw)

        def item(code: str) -> str:
            return str(ChecklistItem.objects.get(section__version=version, code=code).pk)

        # 1. A restaurant execution, in progress: some answers saved, one refused by validation.
        restaurant = assign("DEMO-R-1", priority="high", instructions="Opening checks before service.")
        live, _ = services.start_or_resume(restaurant, actor=actor)
        result = services.save_answers(live, actor=actor, inputs=[
            services.AnswerInput(item("FIRE-EXIT"), False), services.AnswerInput(item("EXTINGUISHER"), "yes"),
            services.AnswerInput(item("TEMP"), "42"),  # out of range: refused, nothing stored for it
        ])
        self.stdout.write(f"{live.reference}: in progress for {restaurant.reference} ({len(result.saved)} answers saved, "
                          f"{len(result.errors)} refused, {services.compute_progress(result.state)['percent']}% complete).")

        # 2. A warehouse execution, completed and submitted - the same engine at a different kind of location.
        warehouse = assign("DEMO-WH-1", priority="normal")
        done, _ = services.start_or_resume(warehouse, actor=actor)
        services.save_answers(done, actor=actor, inputs=[
            services.AnswerInput(item("FIRE-EXIT"), True), services.AnswerInput(item("EXTINGUISHER"), "na"),
            services.AnswerInput(item("TEMP"), "3.5", comment="Checked at opening"), services.AnswerInput(item("TIDY"), "good"),
            services.AnswerInput(item("CHECKED"), timezone.localdate().isoformat()),
        ])
        services.submit(done, actor=actor)
        self.stdout.write(f"{done.reference}: submitted for {warehouse.reference} (read-only).")

        # 3. Work waiting to be started.
        waiting = assign("DEMO-R-2", priority="low")
        self.stdout.write(f"{waiting.reference}: assigned and not started.")
        self.stdout.write(self.style.SUCCESS(f"Execution demo {CODE} created."))

    def _actor(self, organization: Organization):  # noqa: ANN202
        """An existing organization user who may assign and run checklists (never invents credentials)."""
        needed = (P.ASSIGNMENT_CREATE, P.ASSIGNMENT_ISSUE, P.EXECUTION_CREATE, P.EXECUTION_UPDATE, P.EXECUTION_SUBMIT)
        for user in get_user_model().objects.filter(is_active=True, organization_memberships__organization=organization).distinct().order_by("date_joined"):
            if all(has_permission(user, code, organization=organization) for code in needed):
                return user
        raise CommandError("No active user in this organization may assign and run checklists; give someone the Administrator role first.")

