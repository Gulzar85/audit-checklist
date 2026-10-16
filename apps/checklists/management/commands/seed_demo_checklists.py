"""Create demo checklist templates that prove the generic engine (idempotent)."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.checklists import services
from apps.checklists.models import ChecklistTemplate
from apps.organizations.models import Organization

DEMOS = (
    {
        "code": "DEMO-FSI", "name": "Food Safety Inspection", "category": "Food Safety", "minutes": 30,
        "description": "Shows Yes/No, Yes/No/N-A, Score, Numeric and Long-text items side by side.",
        "sections": (
            ("Personal Hygiene", (
                ("Staff wearing clean uniform?", "yes_no", None, ()),
                ("Proper handwashing observed?", "yes_no", None, ()),
            )),
            ("Food Storage", (
                ("Refrigerator temperature", "numeric", {"min": -5, "max": 8, "unit": "°C", "decimals": 1}, ()),
                ("Products correctly labeled?", "yes_no_na", None, ()),
                ("Storage condition", "score", {"min": 1, "max": 5}, ()),
            )),
            ("General Observations", (
                ("Describe any observed issue.", "long_text", None, ()),
            )),
        ),
    },
    {
        "code": "DEMO-SAFE", "name": "Safety Walk", "category": "Safety", "minutes": 15,
        "description": "Boolean and choice items - no scoring anywhere.",
        "sections": (
            ("General Safety", (
                ("Is the emergency exit accessible?", "boolean", None, ()),
                ("Is the storage area clean?", "yes_no_na", None, ()),
                ("Overall condition", "single_choice", None, (("good", "Good"), ("fair", "Fair"), ("poor", "Poor"))),
                ("Hazards present", "multiple_choice", None, (("wet", "Wet floor"), ("blocked", "Blocked exit"), ("none", "None"))),
                ("Upload a photo of the area", "photo", None, ()),
            )),
        ),
    },
)


class Command(BaseCommand):
    help = "Seed demo checklist templates for one organization (safe to run repeatedly)."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("--organization", help="Organization slug (default: the only organization).")

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        organizations = Organization.objects.all()
        if options["organization"]:
            organizations = organizations.filter(slug=options["organization"])
        if organizations.count() != 1:
            raise CommandError("Name exactly one organization with --organization <slug>.")
        organization = organizations.get()
        created = 0
        for demo in DEMOS:
            if ChecklistTemplate.objects.filter(organization=organization, code=demo["code"]).exists():
                continue
            category = services.create_category(organization=organization, name=demo["category"])
            template = services.create_checklist_template(
                organization=organization, name=demo["name"], code=demo["code"], description=demo["description"],
                estimated_duration_minutes=demo["minutes"], category=category,
            )
            draft = template.versions.get()
            for section_name, items in demo["sections"]:
                section = services.create_section(draft, name=section_name)
                for question, answer_type, config, options_ in items:
                    services.create_item(section, question=question, answer_type=answer_type, config=config, options=options_)
            services.publish_checklist_version(draft, change_summary="Initial demo version")
            services.activate_checklist_template(template)
            created += 1
        self.stdout.write(self.style.SUCCESS(f"Demo checklists: {created} created, {len(DEMOS) - created} already present."))