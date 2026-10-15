"""Create clearly-labelled demo data for development and demos.

    python manage.py seed_demo_data

Two demo organizations show that one schema serves very different hierarchies:

    DEMO     Global → Region → Area → Location
    DEMO_BU  Business Unit → Division → Facility → Department

The command is idempotent (re-running adds nothing) and **refuses to run in a
production environment**: demo records must never reach real tenants.  Everything
it creates goes through the same services as the application, so it is logged and
validated like real data.  It creates no users or credentials.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.locations import services as location_services
from apps.locations.models import Location, LocationType
from apps.organizations import services as organization_services
from apps.organizations.models import Organization

DEMO_NOTE = "Demo data. Safe to deactivate."

#: (code, name, description, [(type code, type name)...], tree)
#: A tree node is (name, code, type code, [children...]).
DEMOS = (
    {
        "organization": {"code": "DEMO", "name": "Demo Organization", "country": "GB", "timezone": "Europe/London", "currency": "GBP"},
        "types": (("GLOBAL", "Global"), ("REGION", "Region"), ("AREA", "Area"), ("LOCATION", "Location")),
        "tree": (
            "Global", "GLOBAL", "GLOBAL",
            (
                ("Region A", "REGION-A", "REGION", (
                    ("Area A", "AREA-A", "AREA", (
                        ("Location A", "LOC-A", "LOCATION", ()),
                        ("Location B", "LOC-B", "LOCATION", ()),
                    )),
                )),
                ("Region B", "REGION-B", "REGION", (
                    ("Area B", "AREA-B", "AREA", (
                        ("Location C", "LOC-C", "LOCATION", ()),
                    )),
                )),
            ),
        ),
    },
    {
        "organization": {"code": "DEMO_BU", "name": "Demo Organization (Business Units)", "country": "US", "timezone": "America/New_York", "currency": "USD"},
        "types": (
            ("BUSINESS_UNIT", "Business Unit"),
            ("DIVISION", "Division"),
            ("FACILITY", "Facility"),
            ("DEPARTMENT", "Department"),
        ),
        "tree": (
            "Business Unit 1", "BU-1", "BUSINESS_UNIT",
            (
                ("Division 1", "DIV-1", "DIVISION", (
                    ("Facility 1", "FAC-1", "FACILITY", (
                        ("Department 1", "DEP-1", "DEPARTMENT", ()),
                        ("Department 2", "DEP-2", "DEPARTMENT", ()),
                    )),
                )),
            ),
        ),
    },
)


class Command(BaseCommand):
    help = "Create demo organizations and a sample location hierarchy (never in production)."

    @transaction.atomic
    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        environment = str(getattr(settings, "ENVIRONMENT", "")).lower()
        if environment in {"production", "prod"}:
            raise CommandError("seed_demo_data refuses to run in a production environment.")

        created = 0
        for demo in DEMOS:
            organization, was_created = self._organization(demo["organization"])
            created += was_created
            types = {}
            for order, (code, name) in enumerate(demo["types"]):
                lt, was_created = self._type(organization, code, name, order)
                types[code] = lt
                created += was_created
            created += self._tree(organization, types, demo["tree"], parent=None)

        if created:
            self.stdout.write(self.style.SUCCESS(f"Created {created} demo record(s)."))
        else:
            self.stdout.write("Demo data already present; nothing to do.")

    def _organization(self, data: dict) -> tuple[Organization, bool]:
        existing = Organization.objects.filter(code=data["code"]).first()
        if existing:
            return existing, False
        return organization_services.create_organization(description=DEMO_NOTE, **data), True

    def _type(self, organization: Organization, code: str, name: str, order: int):  # noqa: ANN202
        existing = LocationType.objects.filter(organization=organization, code=code).first()
        if existing:
            return existing, False
        return (
            location_services.create_location_type(
                organization=organization, code=code, name=name, sort_order=order, description=DEMO_NOTE
            ),
            True,
        )

    def _tree(self, organization, types, node, parent) -> int:  # noqa: ANN001
        name, code, type_code, children = node
        created = 0
        location = Location.objects.filter(organization=organization, code=code).first()
        if location is None:
            location = location_services.create_location(
                organization=organization,
                location_type=types[type_code],
                parent=parent,
                name=name,
                code=code,
                description=DEMO_NOTE,
                metadata={"demo": True},
            )
            created += 1
        for child in children:
            created += self._tree(organization, types, child, parent=location)
        return created
