"""Demonstrate checklist versioning end to end on a generic checklist (idempotent, development data).

Creates *Restaurant Daily Safety Checklist* (``DEMO-RSC``) as a published version 1, then - through the same lifecycle
services the builder and API use - creates version 2 from it, tightens the refrigerator range and the fire-exit rule in
the draft, shows that version 1 is untouched, validates and publishes version 2 (version 1 becomes superseded) and prints the
version history and the comparison.  A restaurant is just a location; nothing here is specific to one organization.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.checklists import lifecycle, selectors, services
from apps.checklists.manifest import verify_version_integrity
from apps.checklists.models import ChecklistItem, ChecklistTemplate
from apps.checklists.seed import load_data_file, load_template
from apps.organizations.models import Organization

CODE = "DEMO-RSC"


class Command(BaseCommand):
    help = "Seed the versioning demo (Restaurant Daily Safety Checklist, v1 published, v2 created and published) for one organization."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("--organization", help="Organization slug (default: the only organization).")

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        organizations = Organization.objects.all()
        if options["organization"]:
            organizations = organizations.filter(slug=options["organization"])
        if organizations.count() != 1:
            raise CommandError("Name exactly one organization with --organization <slug>.")
        organization = organizations.get()
        if ChecklistTemplate.objects.filter(organization=organization, code=CODE).exists():
            self.stdout.write(f"Versioning demo {CODE}: already present.")
            return
        template, _created = load_template(organization, load_data_file("restaurant_daily_safety.json"))
        v1 = template.versions.get(version_number=1)
        self.stdout.write(f"Version 1 published ({v1.integrity_hash[:24]}…).")

        v2 = lifecycle.create_draft(template, change_summary="Tighten the refrigerator range; require a manager comment for a blocked exit")
        self.stdout.write(f"Draft version {v2.version_number} created as a copy of version {v2.based_on.version_number}.")
        fridge = ChecklistItem.objects.get(section__version=v2, code="FRIDGE-TEMP")
        services.update_item(fridge, config={"min": 0, "max": 8, "decimals": 1, "unit": "°C"})
        rule = v2.rules.get(code="R-001")
        services.update_rule(rule, actions=[*rule.actions, {"type": "REQUIRE_COMMENT", "target": {"type": "item", "code": "FIRE-EXIT"}, "prompt": "Who was told?"}])

        v1_fridge = ChecklistItem.objects.get(section__version=v1, code="FRIDGE-TEMP")
        self.stdout.write(f"Version 1 is unchanged: refrigerator range still {v1_fridge.config['min']}–{v1_fridge.config['max']} °C "
                          f"(integrity {verify_version_integrity(v1).status}).")

        report = lifecycle.run_validation(v2)
        self.stdout.write(f"Validation of version 2: {'valid' if report.ok else 'INVALID'} ({len(report.warnings)} warning(s)).")
        published = lifecycle.publish_version(v2, expected_hash=report.draft_hash)
        v1.refresh_from_db()
        self.stdout.write(f"Version {published.version_number} published; version 1 is now {v1.status}.")

        self.stdout.write("Version history:")
        for version in selectors.versions_of(template):
            self.stdout.write(f"  v{version.version_number}  {version.status:<10}  {version.section_count} sections, {version.item_count} items  {version.change_summary}")
        comparison = lifecycle.compare_versions(template, v1, published, log=False)
        self.stdout.write("Version 1 → Version 2:")
        for change in comparison.changes:
            self.stdout.write(f"  [{change.kind}] {change.summary}")
        self.stdout.write(self.style.SUCCESS(f"Versioning demo {CODE} created."))
