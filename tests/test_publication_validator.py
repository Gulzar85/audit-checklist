"""The pre-publication validator: errors block, warnings inform, and it reuses the Phase 5 and Phase 6 validators."""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.checklists import lifecycle, publication, services
from apps.checklists.models import ChecklistItem, ChecklistOption, ChecklistRule, ChecklistSection, ChecklistVersion
from apps.checklists.publication import validate_for_publication

from versioning_support import act, build_world, item_of


@pytest.fixture
def world(org_a, org_b, make_user, assign):
    return build_world(org_a, org_b, make_user, assign)


@pytest.fixture
def published(org_a, org_b, make_user, assign):
    return build_world(org_a, org_b, make_user, assign, publish=True)


def report(version):
    return validate_for_publication(ChecklistVersion.objects.select_related("template__organization", "based_on").get(pk=version.pk))


def codes(rep, level="error"):
    return sorted({f.code for f in rep.findings if f.level == level})


@pytest.mark.django_db
class TestValidDrafts:
    def test_a_complete_draft_is_valid_with_stats_and_a_hash(self, world):
        rep = report(world["draft"])
        assert rep.ok and rep.errors == [] and rep.stats == {"sections": 3, "items": 5, "options": 8, "rules": 1}
        assert rep.draft_hash.startswith("sha256:v1:") and rep.template_code == "RDSC" and rep.version_number == 1
        assert codes(rep, "warning") == []  # it has instructions, required items and no earlier version

    def test_as_dict_separates_errors_from_warnings(self, org_a):
        template = services.create_checklist_template(organization=org_a, name="Empty", code="EMP-1")
        payload = report(template.versions.get()).as_dict()
        assert payload["valid"] is False and payload["errors"][0]["code"] == "no_sections"
        assert {w["code"] for w in payload["warnings"]} == {"no_instructions"} and set(payload) >= {"stats", "draft_hash", "version"}

    def test_legacy_messages_contract(self, org_a):
        template = services.create_checklist_template(organization=org_a, name="Legacy", code="LEG-1")
        assert services.validate_version_complete(template.versions.get()) == ["Add at least one section."]


@pytest.mark.django_db
class TestStructureErrors:
    def test_empty_draft_and_empty_section(self, org_a):
        template = services.create_checklist_template(organization=org_a, name="Empty", code="EMP-2")
        draft = template.versions.get()
        assert codes(report(draft)) == ["no_sections"]
        services.create_section(draft, name="Nothing here")
        assert codes(report(draft)) == ["empty_section"]

    def test_invalid_and_duplicate_section_codes_and_order(self, world):
        sections = list(world["draft"].sections.order_by("sort_order"))
        ChecklistSection.objects.filter(pk=sections[0].pk).update(code="bad code!")
        ChecklistSection.objects.filter(pk=sections[2].pk).update(sort_order=9)
        assert {"invalid_section_code", "bad_section_order"} <= set(codes(report(world["draft"])))

    def test_a_missing_name(self, world):
        ChecklistSection.objects.filter(version=world["draft"], code="S-001").update(name="  ")
        assert "section_name_missing" in codes(report(world["draft"]))


@pytest.mark.django_db
class TestItemErrors:
    def test_an_unknown_answer_type(self, world, monkeypatch):
        class Registry:
            def __contains__(self, code):
                return code != "yes_no"

            def clean_config(self, answer_type, config):
                return config

        monkeypatch.setattr(publication, "registry", Registry())
        rep = report(world["draft"])
        assert "unknown_answer_type" in codes(rep) and "FIRE-EXIT" in " ".join(f.message for f in rep.errors)

    def test_an_invalid_configuration_is_reported_by_the_answer_type_engine(self, world):
        ChecklistItem.objects.filter(section__version=world["draft"], code="FRIDGE-TEMP").update(config={"min": 50, "max": 1})
        rep = report(world["draft"])
        assert "invalid_config" in codes(rep) and "FRIDGE-TEMP" in rep.errors[0].message and rep.errors[0].subject == "item:FRIDGE-TEMP"

    def test_a_missing_question_and_invalid_reference_data(self, world):
        ChecklistItem.objects.filter(section__version=world["draft"], code="ISSUE").update(question=" ", metadata={"bogus": 1})
        assert {"question_missing", "invalid_metadata"} <= set(codes(report(world["draft"])))

    def test_duplicate_item_codes_are_found_across_sections(self, world):
        ChecklistItem.objects.filter(section__version=world["draft"], code="ISSUE").update(code="FIRE-EXIT")  # unique per section only in the DB
        rep = report(world["draft"])
        assert "duplicate_item_code" in codes(rep) and "used more than once" in next(f for f in rep.errors if f.code == "duplicate_item_code").message

    def test_invalid_item_code_and_ordering(self, world):
        ChecklistItem.objects.filter(section__version=world["draft"], code="HANDWASH").update(code="bad code")
        ChecklistItem.objects.filter(section__version=world["draft"], code="FRIDGE-TEMP").update(sort_order=8)
        assert {"invalid_item_code", "bad_item_order"} <= set(codes(report(world["draft"])))


@pytest.mark.django_db
class TestOptionErrors:
    def test_a_choice_question_needs_two_options(self, world):
        ChecklistOption.objects.filter(item__section__version=world["draft"], item__code="CONDITION", value__in=["fair", "poor"]).update(is_active=False)
        rep = report(world["draft"])
        assert "too_few_options" in codes(rep) and "needs at least two options" in rep.messages()[0]

    def test_fixed_option_types_must_carry_the_platform_options(self, world):
        ChecklistOption.objects.filter(item__section__version=world["draft"], item__code="FIRE-EXIT", value="no").delete()
        assert "fixed_options_mismatch" in codes(report(world["draft"]))

    def test_types_without_options_must_have_none(self, world):
        ChecklistOption.objects.create(item=item_of(world["draft"], "ISSUE"), value="x", label="X", sort_order=1)
        assert "unexpected_options" in codes(report(world["draft"]))

    def test_invalid_option_values_and_labels_and_order(self, world):
        ChecklistOption.objects.filter(item__section__version=world["draft"], item__code="CONDITION", value="good").update(value="Bad Value!", label=" ")
        ChecklistOption.objects.filter(item__section__version=world["draft"], item__code="CONDITION", value="poor").update(sort_order=9)
        assert {"invalid_option_value", "option_label_missing", "bad_option_order"} <= set(codes(report(world["draft"])))


@pytest.mark.django_db
class TestRuleErrors:
    def test_rule_problems_use_the_phase_6_validator(self, world):
        ChecklistRule.objects.filter(version=world["draft"]).update(
            condition={"source_item_code": "GONE", "comparison": "EQUALS", "value": "no"})
        rep = report(world["draft"])
        error = next(f for f in rep.errors if f.code == "unknown_item")
        assert error.subject == "rule:R-001" and error.message.startswith("Rule R-001:")

    def test_an_operator_that_does_not_suit_the_answer_type(self, world):
        ChecklistRule.objects.filter(version=world["draft"]).update(
            condition={"source_item_code": "FIRE-EXIT", "comparison": "GREATER_THAN", "value": 3})
        assert "invalid_operator" in codes(report(world["draft"]))

    def test_a_cycle_is_a_blocking_error(self, world):
        author = world["author"]
        services.create_rule(world["draft"], name="a", actor=author, code="R-A", condition={"source_item_code": "FIRE-EXIT", "comparison": "EQUALS", "value": "no"},
                             actions=[act("SHOW_ITEM", "HANDWASH")])
        ChecklistRule.objects.create(version=world["draft"], code="R-B", name="b", sort_order=3, condition={"source_item_code": "HANDWASH", "comparison": "EQUALS", "value": "yes"},
                                     actions=[act("SHOW_ITEM", "FIRE-EXIT")])
        rep = report(world["draft"])
        assert "cycle" in codes(rep) and "circle" in next(f for f in rep.errors if f.code == "cycle").message

    def test_possible_conflicts_are_warnings_not_errors(self, world):
        services.create_rule(world["draft"], name="hide", actor=world["author"], condition={"source_item_code": "HANDWASH", "comparison": "EQUALS", "value": "no"},
                             actions=[act("HIDE_ITEM", "ISSUE")])
        rep = report(world["draft"])
        assert rep.ok and "possible_conflict" in codes(rep, "warning")

    def test_a_rule_cannot_reference_another_versions_question(self, published):
        v2 = lifecycle.create_draft(published["template"], actor=published["author"])
        services.delete_item(item_of(v2, "HANDWASH"), actor=published["author"])  # exists in v1, removed in v2
        ChecklistRule.objects.filter(version=v2).update(condition={"source_item_code": "HANDWASH", "comparison": "EQUALS", "value": "no"})
        assert "unknown_item" in codes(report(v2))  # v1 still has it, but a v2 rule resolves only inside v2


@pytest.mark.django_db
class TestTemplateAndVersionErrors:
    def test_an_archived_template(self, world):
        services.archive_checklist_template(world["template"], actor=world["author"])
        assert "template_archived" in codes(report(world["draft"]))

    def test_a_non_draft_version(self, published):
        assert "not_draft" in codes(report(published["v1"]))

    def test_a_base_version_of_another_template(self, published, org_a):
        v2 = lifecycle.create_draft(published["template"], actor=published["author"])
        ChecklistVersion.objects.filter(pk=v2.pk).update(based_on=published["foreign_version"])
        assert "invalid_base" in codes(report(v2))

    def test_an_inactive_organization(self, world, org_a):
        from apps.organizations.models import Organization

        Organization.objects.filter(pk=org_a.pk).update(status="suspended")
        assert "organization_inactive" in codes(report(world["draft"]))


@pytest.mark.django_db
class TestWarnings:
    def test_instructions_optional_and_inactive_warnings(self, world):
        author, draft = world["author"], world["draft"]
        services.update_checklist_template(world["template"], actor=author, instructions="")
        ChecklistItem.objects.filter(section__version=draft).update(is_required=False)
        ChecklistItem.objects.filter(section__version=draft, code="HANDWASH").update(is_active=False)
        rep = report(draft)
        assert rep.ok and {"no_instructions", "all_optional", "inactive_items"} <= set(codes(rep, "warning"))

    def test_warnings_do_not_block_and_are_stored_with_the_version(self, world):
        services.update_checklist_template(world["template"], actor=world["author"], instructions="")
        version = lifecycle.publish_version(world["draft"], actor=world["author"])
        assert {w["code"] for w in version.publication_report["warnings"]} == {"no_instructions"}

    def test_no_change_summary_and_identical_draft(self, published):
        v2 = lifecycle.create_draft(published["template"], actor=published["author"])
        rep = report(v2)
        assert {"no_change_summary", "no_changes"} <= set(codes(rep, "warning")) and rep.ok
        services.update_item(item_of(v2, "FIRE-EXIT"), actor=published["author"], question="Is every fire exit clear?")
        assert "no_changes" not in codes(report(v2), "warning")

    def test_blocking_errors_stop_publication_and_non_blocking_warnings_do_not(self, world):
        ChecklistItem.objects.filter(section__version=world["draft"], code="FRIDGE-TEMP").update(config={"min": 50, "max": 1})
        with pytest.raises(lifecycle.PublicationBlocked) as info:
            lifecycle.publish_version(world["draft"], actor=world["author"])
        assert any(f.code == "invalid_config" for f in info.value.report.errors)
        ChecklistItem.objects.filter(section__version=world["draft"], code="FRIDGE-TEMP").update(config={"min": 0, "max": 10, "decimals": 1, "unit": "°C"})
        assert lifecycle.publish_version(world["draft"], actor=world["author"]).status == "published"


@pytest.mark.django_db
class TestPerformance:
    def test_the_validator_uses_a_fixed_number_of_queries(self, world):
        small = CaptureQueriesContext(connection)
        with small:
            report(world["draft"])
        section = world["draft"].sections.first()
        for n in range(30):
            services.create_item(section, question=f"Extra {n}", answer_type="single_choice", actor=world["author"], options=[("a", "A"), ("b", "B")])
        large = CaptureQueriesContext(connection)
        with large:
            report(world["draft"])
        assert len(large) <= len(small) + 1 and len(large) < 25
