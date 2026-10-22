"""Demonstrate checklist assignment end to end on generic data (idempotent, development data only).

Builds a small hierarchy (Region > Area > Restaurant, plus a Warehouse - a restaurant is just a location), publishes the
*Restaurant Daily Safety Checklist* version 1 under its own demo code, then - through the same lifecycle services the UI and
API use - creates assignments for two restaurants (one saved as a draft, then issued), a warehouse assignment, and a cancelled
assignment so the history shows every kind of event.  Nothing here is specific to one organization.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.assignments import services
from apps.assignments.models import ChecklistAssignment
from apps.checklists.models import ChecklistTemplate
from apps.checklists.seed import load_data_file, load_template
from apps.locations import services as location_services
from apps.locations.models import Location, LocationType
from apps.organizations.models import Organization

CODE = "DEMO-ASG-RSC"

HIERARCHY = (  # (code, name, type, parent code)
    ("DEMO-REG-N", "Demo Region North", "Region", None),
    ("DEMO-AREA-1", "Demo Area 1", "Area", "DEMO-REG-N"),
    ("DEMO-R-1", "Demo Restaurant 1", "Restaurant", "DEMO-AREA-1"),
    ("DEMO-R-2", "Demo Restaurant 2", "Restaurant", "DEMO-AREA-1"),
    ("DEMO-R-3", "Demo Restaurant 3", "Restaurant", "DEMO-AREA-1"),
    ("DEMO-WH-1", "Demo Warehouse 1", "Warehouse", None),
)


class Command(BaseCommand):
    help = "Seed the assignment demo (hierarchy, checklist v1, draft/issued/cancelled assignments) for one organization."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("--organization", help="Organization slug (default: the only organization).")

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        organizations = Organization.objects.all()
        if options["organization"]:
            organizations = organizations.filter(slug=options["organization"])
        if organizations.count() != 1:
            raise CommandError("Name exactly one organization with --organization <slug>.")
        organization = organizations.get()
        if ChecklistAssignment.objects.filter(organization=organization, version__template__code=CODE).exists():
            self.stdout.write(f"Assignment demo {CODE}: already present.")
            return

        locations = self._hierarchy(organization)
        data = load_data_file("restaurant_daily_safety.json")
        data["code"] = CODE  # its own template: independent of the versioning demo
        template, _created = load_template(organization, data)
        version = template.versions.get(version_number=1)
        self.stdout.write(f"Checklist {template.name} version {version.version_number} is published.")

        due = timezone.now() + timedelta(days=2)
        r1, r2, r3, wh = (locations[c] for c in ("DEMO-R-1", "DEMO-R-2", "DEMO-R-3", "DEMO-WH-1"))

        # The demo needs an authorized acting user: an existing administrator of the organization.
        actor = self._actor(organization)
        draft = services.create_assignment(actor=actor, version=version, location=r1, priority="high", due_at=due,
                                           instructions="Opening checks before service.")
        self.stdout.write(f"{draft.reference}: draft for {r1.name}.")
        services.issue(draft, actor=actor)
        self.stdout.write(f"{draft.reference}: issued.")

        second = services.create_assignment(actor=actor, version=version, location=r2, priority="normal", due_at=due + timedelta(days=1), issue=True)
        self.stdout.write(f"{second.reference}: created and issued for {r2.name}.")
        warehouse = services.create_assignment(actor=actor, version=version, location=wh, priority="low", issue=True,
                                               instructions="Warehouse walk-through - the same checklist at a different kind of location.")
        self.stdout.write(f"{warehouse.reference}: issued for {wh.name}.")

        cancelled = services.create_assignment(actor=actor, version=version, location=r3, priority="normal", issue=True)
        services.cancel(cancelled, actor=actor, reason="Restaurant closed for refurbishment")
        self.stdout.write(f"{cancelled.reference}: cancelled (history kept).")
        services.change_priority(second, actor=actor, priority="critical")
        self.stdout.write(self.style.SUCCESS(f"Assignment demo {CODE} created (4 assignments)."))

    def _hierarchy(self, organization: Organization) -> dict[str, Location]:
        types: dict[str, LocationType] = {}
        for name in ("Region", "Area", "Restaurant", "Warehouse"):
            types[name], _ = LocationType.objects.get_or_create(organization=organization, code=name.upper(), defaults={"name": name})
        made: dict[str, Location] = {}
        for code, name, type_name, parent in HIERARCHY:
            existing = Location.objects.filter(organization=organization, code=code).first()
            made[code] = existing or location_services.create_location(
                organization=organization, name=name, code=code, location_type=types[type_name], parent=made.get(parent) if parent else None)
        return made

    def _actor(self, organization: Organization):  # noqa: ANN202
        """An existing organization administrator (the demo needs an authorized actor; it never invents credentials)."""
        from apps.permissions.authorization import has_permission
        from apps.permissions.registry import P

        from django.contrib.auth import get_user_model

        for user in get_user_model().objects.filter(is_active=True, organization_memberships__organization=organization).distinct().order_by("date_joined"):
            if has_permission(user, P.ASSIGNMENT_CREATE, organization=organization) and has_permission(user, P.ASSIGNMENT_CANCEL, organization=organization):
                return user
        raise CommandError("No active user in this organization may assign checklists; give someone the Administrator role first.")


