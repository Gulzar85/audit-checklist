"""Cloning a version into a draft: independent copies, rebuilt references, stable codes, bounded queries."""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.checklists import lifecycle, services
from apps.checklists.manifest import build_manifest, manifest_hash
from apps.checklists.models import ChecklistItem, ChecklistOption, ChecklistRule, ChecklistSection
from apps.checklists.rules import snapshot_for_version, validate_rule_set

from versioning_support import act, build_world, item_of


@pytest.fixture
def world(org_a, org_b, make_user, assign):
    return build_world(org_a, org_b, make_user, assign, publish=True)


@pytest.fixture
def v2(world):
    return lifecycle.create_draft(world["template"], actor=world["author"])


def ids(model, version):
    qs = {ChecklistSection: {"version": version}, ChecklistRule: {"version": version},
          ChecklistItem: {"section__version": version}, ChecklistOption: {"item__section__version": version}}[model]
    return set(model.objects.filter(**qs).values_list("pk", flat=True))


@pytest.mark.django_db
class TestCloning:
    def test_everything_is_copied(self, world, v2):
        v1 = world["v1"]
        assert build_manifest(v1) == build_manifest(v2)  # same definition...
        assert manifest_hash(v1) == manifest_hash(v2)
        for model in (ChecklistSection, ChecklistItem, ChecklistOption, ChecklistRule):
            assert len(ids(model, v2)) == len(ids(model, v1)) > 0

    def test_every_child_is_a_new_independent_row(self, world, v2):
        for model in (ChecklistSection, ChecklistItem, ChecklistOption, ChecklistRule):
            assert not ids(model, world["v1"]) & ids(model, v2)
        v2_items = ChecklistItem.objects.filter(section__version=v2).select_related("section")
        assert all(item.section.version_id == v2.pk for item in v2_items)  # references were rebuilt onto the new sections
        assert all(o.item.section.version_id == v2.pk for o in ChecklistOption.objects.filter(item__section__version=v2).select_related("item__section"))

    def test_codes_values_order_and_configuration_are_preserved(self, world, v2):
        v1 = world["v1"]
        old = ChecklistItem.objects.filter(section__version=v1).order_by("section__sort_order", "sort_order")
        new = ChecklistItem.objects.filter(section__version=v2).order_by("section__sort_order", "sort_order")
        assert [(i.code, i.answer_type, i.config, i.is_required, i.metadata) for i in old] == [(i.code, i.answer_type, i.config, i.is_required, i.metadata) for i in new]
        old_opts = list(ChecklistOption.objects.filter(item__section__version=v1).order_by("item__code", "sort_order").values_list("item__code", "value", "label", "sort_order", "is_active"))
        new_opts = list(ChecklistOption.objects.filter(item__section__version=v2).order_by("item__code", "sort_order").values_list("item__code", "value", "label", "sort_order", "is_active"))
        assert old_opts == new_opts
        assert [(s.code, s.name, s.sort_order) for s in v1.sections.all()] == [(s.code, s.name, s.sort_order) for s in v2.sections.all()]

    def test_rules_are_copied_and_still_valid(self, world, v2):
        old, new = world["v1"].rules.get(), v2.rules.get()
        assert new.pk != old.pk and (new.code, new.condition, new.actions, new.is_active) == (old.code, old.condition, old.actions, old.is_active)
        assert validate_rule_set(snapshot_for_version(v2)).ok  # references are by code, so they resolve inside the new version

    def test_the_copy_records_its_origin(self, world, v2):
        assert v2.based_on_id == world["v1"].pk and v2.version_number == 2 and v2.created_by_id == world["author"].pk

    def test_editing_the_draft_never_touches_the_source(self, world, v2):
        author, v1 = world["author"], world["v1"]
        before = manifest_hash(v1)
        services.update_item(item_of(v2, "FIRE-EXIT"), actor=author, question="Is every fire exit clear?")
        services.update_item(item_of(v2, "FRIDGE-TEMP"), actor=author, config={"min": 0, "max": 8, "decimals": 1, "unit": "°C"})
        services.create_option(item_of(v2, "CONDITION"), label="Excellent", actor=author)
        services.update_rule(v2.rules.get(), actor=author, name="Renamed rule")
        services.create_section(v2, name="Extra", actor=author)
        assert item_of(v1, "FIRE-EXIT").question == "Is the fire exit accessible?"
        assert item_of(v1, "FRIDGE-TEMP").config["max"] == 10
        assert item_of(v1, "CONDITION").options.count() == 3 and v1.rules.get().name == "Blocked fire exit" and v1.sections.count() == 3
        assert manifest_hash(v1) == before != manifest_hash(v2)

    def test_the_config_is_not_a_shared_object(self, world, v2):
        old, new = item_of(world["v1"], "FRIDGE-TEMP"), item_of(v2, "FRIDGE-TEMP")
        new.config["max"] = 99
        assert old.config["max"] == 10
        old.refresh_from_db()
        assert old.config["max"] == 10

    def test_history_rows_exist_for_the_cloned_children(self, world, v2):
        item = item_of(v2, "FIRE-EXIT")
        assert item.history.count() == 1 and item.history.first().history_user_id == world["author"].pk
        assert item.history.first().history_change_reason == "checklist_version.cloned"

    def test_duplicating_a_template_uses_the_same_cloner(self, world):
        copy = services.duplicate_checklist_template(world["template"], actor=world["author"], name="Copy", code="RDSC-COPY")
        version = copy.versions.get()
        assert version.based_on_id is None and manifest_hash(version) == manifest_hash(world["v1"])
        assert not ids(ChecklistItem, version) & ids(ChecklistItem, world["v1"])


@pytest.mark.django_db
class TestCloningScales:
    def test_the_query_count_does_not_grow_with_the_checklist(self, world):
        author, template = world["author"], world["template"]
        with CaptureQueriesContext(connection) as small:
            draft = lifecycle.create_draft(template, actor=author)  # 5 items, 8 options
        section = draft.sections.first()
        for n in range(40):
            services.create_item(section, question=f"Extra question {n}", answer_type="single_choice", actor=author, options=[("a", "A"), ("b", "B")])
        services.publish_checklist_version(draft, actor=author)
        with CaptureQueriesContext(connection) as large:
            lifecycle.create_draft(template, actor=author)  # 45 items, 88 options
        assert abs(len(large) - len(small)) <= 2, (len(small), len(large))
        assert len(large) < 40
