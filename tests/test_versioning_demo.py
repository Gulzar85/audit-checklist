"""The versioning demo (Restaurant Daily Safety Checklist) and the maintenance commands."""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command

from apps.checklists import lifecycle, selectors
from apps.checklists.manifest import verify_version_integrity
from apps.checklists.models import ChecklistItem, ChecklistTemplate


def seed(org):
    out = io.StringIO()
    call_command("seed_demo_versioning", organization=org.slug, stdout=out)
    return out.getvalue()


@pytest.mark.django_db
class TestVersioningDemo:
    def test_the_walkthrough(self, org_a):
        text = seed(org_a)
        template = ChecklistTemplate.objects.get(organization=org_a, code="DEMO-RSC")
        v1, v2 = template.versions.order_by("version_number")
        assert (v1.status, v2.status) == ("superseded", "published") and v2.based_on_id == v1.pk and template.status == "active"
        assert v1.published_at and v2.published_at and v1.superseded_at and v1.integrity_hash and v2.integrity_hash != v1.integrity_hash
        fridge = {v.version_number: ChecklistItem.objects.get(section__version=v, code="FRIDGE-TEMP").config["max"] for v in (v1, v2)}
        assert fridge == {1: 10, 2: 8}  # version 2 changed the range; version 1 is untouched
        assert verify_version_integrity(v1).ok and verify_version_integrity(v2).ok
        assert "Version 1 is unchanged" in text and "Version 2 published; version 1 is now superseded" in text
        assert "[modified]" in text and "10 → 8" in text and "Version 1 → Version 2" in text

    def test_the_sample_content_is_generic(self, org_a):
        seed(org_a)
        template = ChecklistTemplate.objects.get(organization=org_a, code="DEMO-RSC")
        v1 = template.versions.get(version_number=1)
        assert [s.name for s in v1.sections.order_by("sort_order")] == ["Fire Safety", "Food Storage", "General Operations"]
        types = dict(ChecklistItem.objects.filter(section__version=v1).values_list("code", "answer_type"))
        assert types == {"FIRE-EXIT": "yes_no", "HANDWASH": "yes_no_na", "FRIDGE-TEMP": "numeric", "ISSUE": "long_text"}
        rule = v1.rules.get()
        assert rule.condition["conditions"][0] == {"source_item_code": "FIRE-EXIT", "comparison": "EQUALS", "value": "no"}
        assert {a["type"] for a in rule.actions} == {"SHOW_ITEM", "REQUIRE_ITEM"}

    def test_it_is_idempotent(self, org_a):
        seed(org_a)
        assert "already present" in seed(org_a)
        assert ChecklistTemplate.objects.filter(organization=org_a, code="DEMO-RSC").count() == 1
        assert ChecklistTemplate.objects.get(code="DEMO-RSC").versions.count() == 2

    def test_history_and_comparison_are_available_for_the_demo(self, org_a):
        seed(org_a)
        template = ChecklistTemplate.objects.get(code="DEMO-RSC")
        rows = list(selectors.versions_of(template))
        assert [(r.version_number, r.status, r.section_count, r.item_count) for r in rows] == [(2, "published", 3, 4), (1, "superseded", 3, 4)]
        comparison = lifecycle.compare_versions(template, rows[1], rows[0], log=False)
        fields = {c.field for c in comparison.changes}
        assert "config.max" in fields and "actions" in fields

    def test_it_demands_exactly_one_organization(self, org_a, org_b):
        with pytest.raises(Exception, match="exactly one organization"):
            call_command("seed_demo_versioning")


@pytest.mark.django_db
class TestMaintenanceCommands:
    def test_verify_is_clean_for_untouched_versions(self, org_a):
        seed(org_a)
        out = io.StringIO()
        call_command("verify_checklist_integrity", stdout=out)
        assert "2 version(s) verified" in out.getvalue()

    def test_strict_verification_flags_unsealed_versions(self, org_a):
        from apps.checklists import guards
        from apps.checklists.models import ChecklistVersion

        seed(org_a)
        with guards.allow_definition_changes():
            ChecklistVersion.objects.filter(version_number=1, template__code="DEMO-RSC").update(integrity_hash="")
        call_command("verify_checklist_integrity", stdout=io.StringIO())  # not strict: fine
        with pytest.raises(Exception, match="failed verification"):
            call_command("verify_checklist_integrity", strict=True, stdout=io.StringIO(), stderr=io.StringIO())
        out = io.StringIO()
        call_command("seal_checklist_versions", organization=org_a.slug, stdout=out)
        assert "Sealed 1 version" in out.getvalue()
        call_command("verify_checklist_integrity", strict=True, stdout=io.StringIO())
