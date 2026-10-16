"""Create demo checklist templates that prove the generic engine (idempotent)."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.checklists import services
from apps.checklists.models import ChecklistTemplate
from apps.checklists.seed import load_data_file, load_template
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
        "code": "DEMO-RDSC", "name": "Restaurant Daily Safety Check", "category": "Safety", "minutes": 20,
        "description": "A daily restaurant check using seven different answer types. Any organization can build the same "
                       "with its own location types - a restaurant is just a location.",
        "sections": (
            ("Daily check", (
                ("Is the fire exit accessible?", "yes_no", None, ()),
                ("Is the handwashing station operational?", "yes_no_na", None, ()),
                ("Rate dining area cleanliness.", "score", {"min": 1, "max": 5, "step": 1, "display": "stars"}, ()),
                ("Record refrigerator temperature.", "numeric", {"min": -10, "max": 20, "decimals": 1, "unit": "°C"}, ()),
                ("Which issues were observed?", "multiple_choice", None, (
                    ("cleaning", "Cleaning"), ("maintenance", "Maintenance"), ("safety", "Safety"),
                    ("documentation", "Documentation"), ("training", "Training"))),
                ("Manager comments.", "long_text", None, ()),
                ("Inspection date.", "date", None, ()),
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
        self.ensure_demo_location(organization)
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
        # The conditional-rules demo is a data file, loaded exactly like an organization's own checklist would be.
        _template, rules_created = load_template(organization, load_data_file("daily_facility_safety.json"))
        created += int(rules_created)
        total = len(DEMOS) + 1
        self.stdout.write(self.style.SUCCESS(f"Demo checklists: {created} created, {total - created} already present."))

    def ensure_demo_location(self, organization) -> None:  # noqa: ANN001
        """A ``Restaurant`` location type and a demo restaurant: nothing restaurant-specific is built into the schema."""
        from apps.locations import services as location_services
        from apps.locations.models import Location, LocationType

        location_type = LocationType.objects.filter(organization=organization, code="RESTAURANT").first() or (
            location_services.create_location_type(organization=organization, name="Restaurant", code="RESTAURANT")
        )
        if not Location.objects.filter(organization=organization, code="MCD-DEMO").exists():
            location_services.create_location(
                organization=organization, name="McDonald's Demo Restaurant", code="MCD-DEMO", location_type=location_type
            )
