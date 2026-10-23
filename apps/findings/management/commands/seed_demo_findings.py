"""Demonstrate findings end to end on generic data (idempotent, development data only).

Publishes a *Site Inspection* checklist whose rules raise findings (a blocked exit -> safety/high, a low cleanliness score ->
quality/medium, a leak -> equipment/high needing review), submits one execution through the real services so the findings are
raised by the server, moves one finding through its lifecycle and reports one manual finding.  Nothing is specific to an industry.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.assignments import services as assignment_services
from apps.checklists.models import ChecklistItem
from apps.checklists.seed import load_template
from apps.executions import services as exec_services
from apps.executions.management.commands.seed_demo_executions import Command as ExecutionsDemo
from apps.findings import services
from apps.findings.models import Finding, FindingCategory, FindingSeverity
from apps.locations.models import Location
from apps.organizations.models import Organization

CODE = "DEMO-FND"


def _raise(code: str, **params):  # noqa: ANN202
    return {"type": "RAISE_FINDING", "target": {"type": "item", "code": code}, **params}


CHECKLIST = {
    "code": CODE, "name": "Site Inspection", "category": "Safety", "minutes": 10,
    "description": "A generic inspection whose rules raise findings.",
    "sections": [{"code": "INSPECT", "name": "Inspection", "items": [
        {"code": "EXIT", "question": "Is the emergency exit accessible?", "answer_type": "yes_no"},
        {"code": "CLEAN", "question": "Cleanliness rating", "answer_type": "score", "config": {"min": 1, "max": 5, "step": 1}},
        {"code": "TAGS", "question": "Problems seen", "answer_type": "multiple_choice", "required": False,
         "options": [["leak", "Leak"], ["pest", "Pests"], ["dirt", "Dirt"]], "config": {"max_select": 3}},
        {"code": "NOTE", "question": "Describe what you found", "answer_type": "long_text", "required": False},
    ]}],
    "rules": [
        {"name": "Blocked exit", "condition": {"source_item_code": "EXIT", "comparison": "EQUALS", "value": "no"},
         "actions": [_raise("EXIT", category="safety", severity="high", title="Blocked: {question}", description="The answer was {answer} at {location}.")]},
        {"name": "Poor cleanliness", "condition": {"source_item_code": "CLEAN", "comparison": "LESS_THAN", "value": 3},
         "actions": [_raise("CLEAN", category="quality", severity="medium")]},
        {"name": "Leak seen", "condition": {"source_item_code": "TAGS", "comparison": "CONTAINS_ANY", "value": ["leak"]},
         "actions": [_raise("TAGS", category="equipment", severity="high", requires_review=True)]},
    ],
}


class Command(BaseCommand):
    help = "Seed the findings demo (a rule-driven checklist, one submitted execution, findings in several states)."

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
        if Finding.objects.filter(organization=organization, rule_name="Blocked exit").exists():
            self.stdout.write(f"Findings demo {CODE}: already present.")
            return
        actor = ExecutionsDemo()._actor(organization)
        services.ensure_defaults(organization)
        template, _created = load_template(organization, CHECKLIST)
        version = template.versions.get(version_number=1)
        location = Location.objects.filter(organization=organization, code="DEMO-R-2").first() or Location.objects.filter(organization=organization).first()

        def item(code: str) -> str:
            return str(ChecklistItem.objects.get(section__version=version, code=code).pk)

        assignment = assignment_services.create_assignment(actor=actor, version=version, location=location, issue=True, priority="high",
                                                           due_at=timezone.now() + timedelta(days=2), instructions="Quarterly inspection.")
        execution, _ = exec_services.start_or_resume(assignment, actor=actor)
        exec_services.save_answers(execution, actor=actor, inputs=[
            exec_services.AnswerInput(item("EXIT"), "no"), exec_services.AnswerInput(item("CLEAN"), 2),
            exec_services.AnswerInput(item("TAGS"), ["leak"]),
        ])
        # The follow-up note is only needed because of the blocked exit; the rules above need no extra answers.
        done = exec_services.submit(execution, actor=actor).execution
        raised = list(Finding.objects.filter(execution=done).order_by("number"))
        self.stdout.write(f"{done.reference} was submitted: {len(raised)} finding(s) raised ({', '.join(f.reference for f in raised)}).")
        first = raised[0]
        services.assign(first, actor=actor, assigned_to=actor)
        services.transition(first, "acknowledge", actor=actor)
        services.transition(first, "start", actor=actor)
        services.transition(first, "resolve", actor=actor, reason="The stacked boxes were removed and the aisle marked.")
        manual = services.create_manual(
            actor=actor, location=location, title="Cracked floor tile near the entrance", description="Reported during the walk-through.",
            category=FindingCategory.objects.get(organization=organization, code="safety"), severity=FindingSeverity.objects.get(organization=organization, code="low"))
        self.stdout.write(f"{first.reference} is resolved (not yet closed); {manual.reference} was reported manually.")
        self.stdout.write(self.style.SUCCESS(f"Findings demo {CODE} created. Open /findings/."))
