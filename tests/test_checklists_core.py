"""Checklist definition engine: models, services, selectors and policies."""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.checklists import constants as c
from apps.checklists import policies, selectors, services
from apps.checklists.models import ChecklistItem, ChecklistOption, ChecklistTemplate, ChecklistVersion
from apps.core.models import ActivityLog
from apps.permissions.registry import P


@pytest.fixture
def author(make_user, assign, org_a):
    user = make_user(email="author@example.com", organization=org_a, role=None)
    assign(user, org_a, "administrator")
    return user


@pytest.fixture
def template(org_a, author):
    return services.create_checklist_template(organization=org_a, name="Food Safety", code="fs-001", actor=author)


@pytest.fixture
def draft(template):
    return template.versions.get()


@pytest.fixture
def section(draft, author):
    return services.create_section(draft, name="Storage", actor=author)


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestTemplate:
    def test_create_makes_a_draft_version_one(self, template, author):
        assert template.code == "FS-001" and template.slug == "food-safety"
        assert template.status == c.TemplateStatus.DRAFT
        version = template.versions.get()
        assert version.version_number == 1 and version.is_draft
        assert template.created_by == author
        assert ActivityLog.objects.filter(verb="checklist.created", organization=template.organization).exists()
        assert template.history.count() == 1

    def test_code_is_unique_per_organization_and_normalised(self, org_a, org_b, template, author):
        with pytest.raises(ValidationError):
            services.create_checklist_template(organization=org_a, name="Other", code="FS-001", actor=author)
        services.create_checklist_template(organization=org_b, name="Food Safety", code="FS-001", actor=author)

    def test_invalid_code_is_rejected(self, org_a):
        with pytest.raises(ValidationError):
            services.create_checklist_template(organization=org_a, name="X", code="bad code!!")

    def test_slug_is_unique_but_code_is_the_identity(self, org_a, template, author):
        other = services.create_checklist_template(organization=org_a, name="Food Safety", code="FS-002", actor=author)
        assert other.slug == "food-safety-2"

    def test_rename_keeps_code_and_slug(self, template, author):
        services.update_checklist_template(template, name="Renamed", actor=author, code="HACK-1")
        template.refresh_from_db()
        assert template.name == "Renamed" and template.code == "FS-001" and template.slug == "food-safety"

    def test_category_must_belong_to_the_organization(self, org_b, template, author):
        foreign = services.create_category(organization=org_b, name="Safety")
        with pytest.raises(ValidationError):
            services.update_checklist_template(template, category=foreign, actor=author)

    def test_categories_are_organization_defined_and_idempotent(self, org_a):
        a = services.create_category(organization=org_a, name="Food Safety")
        b = services.create_category(organization=org_a, name="food safety")
        assert a.pk == b.pk

    def test_inactive_organization_cannot_get_checklists(self, org_a, author):
        org_a.status = org_a.Status.SUSPENDED
        org_a.save()
        with pytest.raises(ValidationError):
            services.create_checklist_template(organization=org_a, name="X", code="X-1")

    def test_activate_needs_a_published_version(self, template, section, author):
        with pytest.raises(ValidationError):
            services.activate_checklist_template(template)
        services.create_item(section, question="Clean?", actor=author)
        services.publish_checklist_version(template.versions.get(), actor=author)
        services.activate_checklist_template(template, actor=author)
        template.refresh_from_db()
        assert template.status == c.TemplateStatus.ACTIVE

    def test_archive_and_restore_never_delete(self, template, author):
        services.archive_checklist_template(template, actor=author)
        template.refresh_from_db()
        assert template.status == c.TemplateStatus.ARCHIVED
        with pytest.raises(ValidationError):
            services.update_checklist_template(template, name="No", actor=author)
        services.restore_checklist_template(template, actor=author)
        template.refresh_from_db()
        assert template.status == c.TemplateStatus.DRAFT  # nothing published yet
        assert ChecklistTemplate.objects.filter(pk=template.pk).exists()

    def test_delete_only_unpublished_drafts(self, org_a, author, template, section):
        services.create_item(section, question="Q", actor=author)
        services.publish_checklist_version(template.versions.get(), actor=author)
        with pytest.raises(ValidationError):
            services.delete_checklist_template(template, actor=author)
        scratch = services.create_checklist_template(organization=org_a, name="Scratch", code="SC-1", actor=author)
        services.delete_checklist_template(scratch, actor=author)
        assert not ChecklistTemplate.objects.filter(pk=scratch.pk).exists()

    def test_status_and_version_constraints(self, template):
        with pytest.raises(IntegrityError), transaction.atomic():
            ChecklistTemplate.objects.filter(pk=template.pk).update(status="weird")
        with pytest.raises(IntegrityError), transaction.atomic():
            ChecklistVersion.objects.create(template=template, version_number=2)  # a second draft

    def test_duplicate_copies_structure_with_new_identifiers(self, template, section, author):
        item = services.create_item(section, question="Pick", answer_type="single_choice", actor=author,
                                    options=[("a", "A"), ("b", "B")])
        services.create_item(section, question="Rate", answer_type="score", config={"min": 1, "max": 10}, actor=author)
        copy = services.duplicate_checklist_template(template, actor=author)
        assert copy.pk != template.pk and copy.code == "FS-001-COPY" and copy.name == "Food Safety - Copy"
        assert copy.status == c.TemplateStatus.DRAFT
        copied = ChecklistItem.objects.filter(section__version__template=copy)
        assert copied.count() == 2 and not set(copied.values_list("pk", flat=True)) & {item.pk}
        assert ChecklistOption.objects.filter(item__section__version__template=copy).count() == 2
        assert services.duplicate_checklist_template(template, actor=author).code == "FS-001-COPY-2"
        assert ActivityLog.objects.filter(verb="checklist.duplicated").count() == 2


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestVersionSafety:
    def test_published_version_is_immutable(self, template, section, author):
        item = services.create_item(section, question="Q", actor=author)
        version = template.versions.get()
        services.publish_checklist_version(version, actor=author)
        version.refresh_from_db()
        assert version.status == c.VersionStatus.PUBLISHED and version.published_at
        calls = [
            lambda: services.create_section(version, name="New"),
            lambda: services.update_section(section, name="X"),
            lambda: services.delete_section(section),
            lambda: services.create_item(section, question="Another"),
            lambda: services.update_item(item, question="Changed"),
            lambda: services.delete_item(item),
            lambda: services.reorder_sections(version, [section.pk]),
        ]
        for call in calls:
            with pytest.raises(ValidationError) as info:
                call()
            assert info.value.code == "version_immutable"
        item.refresh_from_db()
        assert item.question == "Q"

    def test_publish_requires_complete_structure(self, template, section, draft):
        with pytest.raises(ValidationError):
            services.publish_checklist_version(draft)  # section without items
        services.create_item(section, question="Choose", answer_type="single_choice", options=[("a", "A")])
        with pytest.raises(ValidationError):
            services.publish_checklist_version(draft)  # one option only

    def test_new_version_is_a_copy_and_leaves_history_alone(self, template, section, author):
        item = services.create_item(section, question="Q", actor=author)
        v1 = template.versions.get()
        services.publish_checklist_version(v1, actor=author)
        v2 = services.create_checklist_version(template, actor=author, change_summary="Tweak")
        assert v2.version_number == 2 and v2.is_draft
        new_item = ChecklistItem.objects.get(section__version=v2)
        assert new_item.pk != item.pk and new_item.code == item.code
        services.update_item(new_item, question="Q (v2)", actor=author)
        item.refresh_from_db()
        assert item.question == "Q"  # v1 untouched
        with pytest.raises(ValidationError):
            services.create_checklist_version(template)  # a draft already exists

    def test_archived_template_cannot_change_structure(self, template, section, author):
        services.archive_checklist_template(template, actor=author)
        with pytest.raises(ValidationError):
            services.create_section(template.versions.get(), name="X")


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestSectionsItemsOptions:
    def test_orders_are_explicit_and_contiguous(self, draft, author):
        s = [services.create_section(draft, name=n, actor=author) for n in ("A", "B", "C")]
        assert [x.sort_order for x in s] == [1, 2, 3]
        services.reorder_sections(draft, [s[2].pk, s[0].pk, s[1].pk], actor=author)
        assert list(draft.sections.order_by("sort_order").values_list("name", flat=True)) == ["C", "A", "B"]
        services.delete_section(s[0], actor=author)
        assert list(draft.sections.order_by("sort_order").values_list("sort_order", flat=True)) == [1, 2]

    def test_reorder_rejects_partial_or_foreign_lists(self, draft, author):
        a = services.create_section(draft, name="A", actor=author)
        services.create_section(draft, name="B", actor=author)
        with pytest.raises(ValidationError):
            services.reorder_sections(draft, [a.pk])
        with pytest.raises(ValidationError):
            services.reorder_sections(draft, [a.pk, a.pk])

    def test_move_up_and_down(self, draft, author):
        a = services.create_section(draft, name="A", actor=author)
        b = services.create_section(draft, name="B", actor=author)
        services.move_section(b, "up", actor=author)
        assert list(draft.sections.order_by("sort_order").values_list("name", flat=True)) == ["B", "A"]
        services.move_section(b, "up", actor=author)  # already first: no-op
        with pytest.raises(ValidationError):
            services.move_section(a, "sideways", actor=author)

    def test_item_codes_are_generated_stable_and_unique_in_the_version(self, template, draft, section, author):
        other = services.create_section(draft, name="Other", actor=author)
        a = services.create_item(section, question="One", actor=author)
        b = services.create_item(other, question="Two", actor=author, code="temp-001")
        assert a.code == "Q-001" and b.code == "TEMP-001"
        with pytest.raises(ValidationError):
            services.create_item(section, question="Dup", code="TEMP-001", actor=author)  # other section, same version
        services.update_item(a, question="Reworded", actor=author)
        a.refresh_from_db()
        assert a.code == "Q-001"

    def test_item_reorder_and_history(self, section, author):
        items = [services.create_item(section, question=f"Q{i}", actor=author) for i in range(3)]
        services.reorder_items(section, [items[2].pk, items[1].pk, items[0].pk], actor=author)
        assert list(section.items.order_by("sort_order").values_list("question", flat=True)) == ["Q2", "Q1", "Q0"]
        assert items[0].history.count() >= 1
        assert ActivityLog.objects.filter(verb="checklist_item.reordered").exists()

    def test_options_only_on_choice_types(self, section, author):
        boolean = services.create_item(section, question="B", answer_type="boolean", actor=author)
        with pytest.raises(ValidationError):
            services.create_option(boolean, label="Maybe")
        choice = services.create_item(section, question="C", answer_type="single_choice", actor=author)
        o1 = services.create_option(choice, label="Excellent", actor=author)
        o2 = services.create_option(choice, label="Good", actor=author)
        assert (o1.value, o2.value, o2.sort_order) == ("excellent", "good", 2)
        with pytest.raises(ValidationError):
            services.create_option(choice, label="Excellent")
        services.move_option(o2, "up", actor=author)
        assert list(choice.options.order_by("sort_order").values_list("value", flat=True)) == ["good", "excellent"]
        services.update_option(o1, label="Outstanding", actor=author, value="hack")
        o1.refresh_from_db()
        assert o1.label == "Outstanding" and o1.value == "excellent"  # value is stable
        services.delete_option(o1, actor=author)
        assert choice.options.count() == 1 and choice.options.get().sort_order == 1

    def test_fixed_option_types_get_their_options(self, section, author):
        yn = services.create_item(section, question="YN", answer_type="yes_no", actor=author)
        ynna = services.create_item(section, question="YNNA", answer_type="yes_no_na", actor=author)
        assert list(yn.options.values_list("value", flat=True)) == ["yes", "no"]
        assert list(ynna.options.values_list("value", flat=True)) == ["yes", "no", "na"]
        with pytest.raises(ValidationError):
            services.create_option(yn, label="Maybe")
        services.update_item(yn, answer_type="yes_no_na", actor=author)
        assert yn.options.count() == 3
        services.update_item(yn, answer_type="numeric", actor=author)
        assert yn.options.count() == 0

    def test_answer_type_change_validates_config(self, section, author):
        item = services.create_item(section, question="Rate", answer_type="score", actor=author)
        assert item.config == {"min": 1, "max": 5, "step": 1, "display": "numeric"}
        with pytest.raises(ValidationError):
            services.update_item(item, answer_type="boolean", config={"min": 1}, actor=author)
        services.update_item(item, answer_type="numeric", actor=author)
        item.refresh_from_db()
        assert item.answer_type == "numeric" and item.config == {"decimals": 2}

    def test_unknown_answer_type(self, section):
        with pytest.raises(ValidationError):
            services.create_item(section, question="Q", answer_type="telepathy")

    def test_delete_item_closes_the_gap(self, section, author):
        items = [services.create_item(section, question=f"Q{i}", actor=author) for i in range(3)]
        services.delete_item(items[0], actor=author)
        assert list(section.items.order_by("sort_order").values_list("sort_order", flat=True)) == [1, 2]


# ---------------------------------------------------------------------------
class TestAnswerConfig:
    @pytest.mark.parametrize("answer_type,config,expected", [
        ("boolean", {}, {}),
        ("score", None, {"min": 1, "max": 5, "step": 1, "display": "numeric"}),
        ("score", {"min": 0, "max": 10}, {"min": 0, "max": 10, "step": 1, "display": "numeric"}),
        ("numeric", {"min": 0, "max": 8.5, "unit": "°C", "decimals": 1}, {"min": 0, "max": 8.5, "unit": "°C", "decimals": 1}),
        ("long_text", {"max_length": 500}, {"min_length": 0, "max_length": 500}),
        ("matrix", {"rows": ["A"], "columns": ["X", "Y"]}, {
            "rows": [{"key": "a", "label": "A"}], "columns": [{"key": "x", "label": "X"}, {"key": "y", "label": "Y"}],
            "mode": "single", "require_all_rows": False}),
        ("photo", {}, {"max_files": 1, "max_size_mb": 10, "allowed_extensions": ["jpg", "jpeg", "png", "webp", "heic"], "source": "any"}),
    ])
    def test_valid(self, answer_type, config, expected):
        assert c.clean_config(answer_type, config) == expected

    @pytest.mark.parametrize("answer_type,config", [
        ("score", {"min": 5, "max": 1}), ("score", {"min": 1, "max": 500}), ("score", {"min": "a", "max": 3}),
        ("numeric", {"min": 9, "max": 1}), ("numeric", {"decimals": 12}), ("boolean", {"min": 1}),
        ("long_text", {"max_length": 0}), ("matrix", {"rows": [], "columns": ["x"]}), ("yes_no", "nope"),
        ("score", {"min": True, "max": 3}),
    ])
    def test_invalid(self, answer_type, config):
        with pytest.raises(ValidationError):
            c.clean_config(answer_type, config)


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestGenericEngine:
    """One template, one item model: Boolean, Yes/No/N-A, Score, Numeric, Text - no per-type tables."""

    def test_four_checklist_kinds_share_one_schema(self, org_a, author):
        template = services.create_checklist_template(organization=org_a, name="Food Safety Inspection", code="FSI-1", actor=author)
        version = template.versions.get()
        hygiene = services.create_section(version, name="Personal Hygiene", actor=author)
        storage = services.create_section(version, name="Food Storage", actor=author)
        notes = services.create_section(version, name="General Observations", actor=author)
        a = services.create_item(hygiene, question="Is the emergency exit accessible?", answer_type="boolean", actor=author)
        b = services.create_item(storage, question="Is the storage area clean?", answer_type="yes_no_na", actor=author)
        s = services.create_item(storage, question="Rate dining area cleanliness.", answer_type="score", actor=author)
        n = services.create_item(storage, question="Enter refrigerator temperature.", answer_type="numeric",
                                 config={"min": -5, "max": 8, "unit": "°C"}, actor=author)
        t = services.create_item(notes, question="Describe any observed issue.", answer_type="long_text", is_required=False, actor=author)
        assert {type(x) for x in (a, b, s, n, t)} == {ChecklistItem}
        assert [x.answer_type for x in (a, b, s, n, t)] == ["boolean", "yes_no_na", "score", "numeric", "long_text"]
        # No scoring anywhere in the definition: options and items carry no score fields.
        field_names = {f.name for f in ChecklistItem._meta.get_fields()} | {f.name for f in ChecklistOption._meta.get_fields()}
        assert not {"score", "possible_score", "achieved_score", "weight", "points"} & field_names
        services.publish_checklist_version(version, actor=author)

    def test_every_answer_type_can_be_defined(self, section, author):
        for answer_type in c.AnswerType.values:
            config = {"rows": ["r"], "columns": ["c"]} if answer_type == "matrix" else None
            item = services.create_item(section, question=f"Type {answer_type}", answer_type=answer_type, config=config, actor=author)
            assert item.answer_type == answer_type

    def test_rules_are_generic_data(self, draft, section, author):
        source = services.create_item(section, question="Fridge ok?", answer_type="yes_no", actor=author)
        target = services.create_item(section, question="Photo", answer_type="photo", is_required=False, actor=author)
        rule = services.create_rule(
            draft, name="Photo when the fridge fails", actor=author,
            condition={"source_item_code": source.code, "comparison": "EQUALS", "value": "no"},
            actions=[{"type": "SHOW_ITEM", "target": {"type": "item", "code": target.code}},
                     {"type": "REQUIRE_COMMENT", "target": {"type": "item", "code": source.code}}],
        )
        assert rule.code == "R-001" and [a["type"] for a in rule.actions] == ["SHOW_ITEM", "REQUIRE_COMMENT"]
        with pytest.raises(ValidationError):  # references a question that does not exist
            services.create_rule(draft, name="Bad", actions=[{"type": "SHOW_ITEM", "target": {"type": "item", "code": "NOPE"}}], actor=author)
        field_names = {f.name for f in ChecklistItem._meta.get_fields()}
        assert not {"requires_photo_if_no", "requires_comment_if_no"} & field_names


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestSelectorsAndPolicies:
    def test_templates_are_organization_scoped(self, org_a, org_b, template, author, make_user, assign):
        foreign = services.create_checklist_template(organization=org_b, name="Foreign", code="F-1")
        visible = set(selectors.templates_for_user(author).values_list("pk", flat=True))
        assert template.pk in visible and foreign.pk not in visible
        assert selectors.get_template(author, foreign.pk) is None
        assert selectors.get_template(author, "not-a-uuid") is None

    def test_member_without_permission_sees_nothing(self, make_user, org_a, template):
        plain = make_user(organization=org_a, role=None)
        assert not selectors.templates_for_user(plain).exists()
        assert not policies.can_view_template(plain, template)

    def test_view_only_role(self, make_user, assign, org_a, template, draft):
        viewer = make_user(organization=org_a, role=None)
        assign(viewer, org_a, "qa-manager")  # has checklist.view/create/update/publish/manage
        assert policies.can_view_template(viewer, template) and policies.can_update_template(viewer, template)
        reader = make_user(organization=org_a, role=None)
        from apps.permissions import services as rbac

        role = rbac.create_role(organization=org_a, name="Reader", permissions=[P.CHECKLIST_VIEW])
        assign(reader, org_a, role)
        assert policies.can_view_template(reader, template)
        assert not policies.can_update_template(reader, template)
        assert not policies.can_edit_version(reader, draft)
        assert not policies.can_create_template(reader, org_a)
        assert not policies.can_manage_template(reader, template)

    def test_scoped_grant_is_not_an_organization_grant(self, make_user, assign, org_a, make_location, template):
        from apps.permissions.models import MembershipRole
        from apps.permissions import services as rbac

        user = make_user(organization=org_a, role=None)
        role = rbac.create_role(organization=org_a, name="R", permissions=[P.CHECKLIST_VIEW])
        assign(user, org_a, role, scope=MembershipRole.ScopeType.LOCATION, location=make_location(org_a, "L"))
        assert not policies.can_view_template(user, template)  # definitions are organization-level

    def test_published_versions_are_not_editable_by_policy(self, author, template, section, draft):
        services.create_item(section, question="Q", actor=author)
        assert policies.can_edit_version(author, draft)
        services.publish_checklist_version(draft, actor=author)
        draft.refresh_from_db()
        assert not policies.can_edit_version(author, draft)
        assert not policies.can_publish_version(author, draft)

    def test_delete_policy_is_conservative(self, author, template):
        assert policies.can_delete_template(author, template)
        services.archive_checklist_template(template, actor=author)
        template.refresh_from_db()
        assert not policies.can_delete_template(author, template)

    def test_structure_read_uses_constant_queries(self, section, author, draft, django_assert_max_num_queries):
        for i in range(5):
            item = services.create_item(section, question=f"Q{i}", answer_type="single_choice", actor=author,
                                        options=[("a", "A"), ("b", "B")])
        other = services.create_section(draft, name="Other", actor=author)
        services.create_item(other, question="X", actor=author)
        with django_assert_max_num_queries(4):
            rows = list(selectors.get_sections_for_version(draft))
            for sec in rows:
                for it in sec.items.all():
                    list(it.options.all())
        assert len(rows) == 2
