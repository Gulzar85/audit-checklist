"""Comparing real versions (the lifecycle service) - including the semantics a code match must not hide."""

from __future__ import annotations

import pytest

from apps.checklists import lifecycle, services
from apps.checklists.manifest import manifest_hash
from apps.core.models import ActivityLog

from versioning_support import act, build_world, item_of


@pytest.fixture
def world(org_a, org_b, make_user, assign):
    return build_world(org_a, org_b, make_user, assign, publish=True)


def changes(comparison, **where):
    return [c for c in comparison.changes if all(getattr(c, k) == v for k, v in where.items())]


@pytest.mark.django_db
class TestComparingVersions:
    def evolve(self, world):
        author = world["author"]
        v2 = lifecycle.create_draft(world["template"], actor=author)
        services.update_item(item_of(v2, "FRIDGE-TEMP"), actor=author, config={"min": 0, "max": 8, "decimals": 1, "unit": "°C"})
        services.update_item(item_of(v2, "FIRE-EXIT"), actor=author, question="Is every fire exit accessible and unobstructed?")
        services.delete_item(item_of(v2, "HANDWASH"), actor=author)
        added = services.create_item(v2.sections.get(code="S-001"), question="Is the emergency lighting working?", code="LIGHTS", actor=author)
        services.create_option(item_of(v2, "CONDITION"), label="Excellent", actor=author)
        rule = v2.rules.get()
        services.update_rule(rule, actor=author, actions=[*rule.actions, act("REQUIRE_COMMENT", "FIRE-EXIT")])
        services.create_rule(v2, name="Warm fridge", actor=author, condition={"source_item_code": "FRIDGE-TEMP", "comparison": "GREATER_THAN", "value": "5"},
                             actions=[act("DISPLAY_MESSAGE", "FRIDGE-TEMP", message="Getting warm", severity="warning")])
        ids = list(v2.sections.order_by("sort_order").values_list("pk", flat=True))
        services.reorder_sections(v2, [ids[2], ids[0], ids[1]], actor=author)
        lifecycle.publish_version(v2, actor=author)
        return v2, added

    def test_a_realistic_release_is_described_accurately(self, world):
        v2, _added = self.evolve(world)
        result = lifecycle.compare_versions(world["template"], world["v1"], v2, actor=world["author"])
        assert not result.identical
        assert changes(result, category="item", kind="added", code="LIGHTS") and changes(result, category="item", kind="removed", code="HANDWASH")
        assert changes(result, field="config.max")[0].summary.endswith("10 → 8.")
        assert changes(result, field="question", code="FIRE-EXIT") and changes(result, category="option", kind="added")
        assert changes(result, category="rule", kind="added", code="R-002") and changes(result, category="rule", kind="modified", code="R-001", field="actions")
        assert changes(result, category="section", kind="reordered")
        counts = result.counts()
        assert counts["by_category"]["rule"] == 2 and counts["by_impact"]["behavior"] >= 4

    def test_the_comparison_alters_neither_version(self, world):
        v2, _ = self.evolve(world)
        world["v1"].refresh_from_db()
        v2.refresh_from_db()
        before = (manifest_hash(world["v1"]), manifest_hash(v2), world["v1"].status, v2.status, world["v1"].updated_at, v2.updated_at)
        lifecycle.compare_versions(world["template"], world["v1"], v2, actor=world["author"])
        world["v1"].refresh_from_db()
        v2.refresh_from_db()
        assert (manifest_hash(world["v1"]), manifest_hash(v2), world["v1"].status, v2.status, world["v1"].updated_at, v2.updated_at) == before

    def test_a_version_compared_with_itself_is_identical(self, world):
        assert lifecycle.compare_versions(world["template"], world["v1"], world["v1"]).identical

    def test_same_code_different_meaning_is_not_unchanged(self, world):
        author = world["author"]
        v2 = lifecycle.create_draft(world["template"], actor=author)
        services.update_item(item_of(v2, "ISSUE"), actor=author, answer_type="text", question="What is the issue (one line)?")
        result = lifecycle.compare_versions(world["template"], world["v1"], v2, log=False)
        fields = {c.field for c in changes(result, code="ISSUE")}
        assert {"answer_type", "question"} <= fields
        assert changes(result, field="answer_type")[0].impact == "behavior"

    def test_a_code_removed_and_later_reintroduced_for_a_different_question(self, world):
        author = world["author"]
        v2 = lifecycle.create_draft(world["template"], actor=author)
        services.delete_item(item_of(v2, "HANDWASH"), actor=author)
        services.create_item(v2.sections.get(code="S-002"), question="A completely different thing", code="HANDWASH", answer_type="numeric", actor=author)
        result = lifecycle.compare_versions(world["template"], world["v1"], v2, log=False)
        modified = changes(result, code="HANDWASH", kind="modified")
        assert {c.field for c in modified} >= {"question", "answer_type"}  # reported as modified, never silently "the same question"

    def test_versions_of_different_templates_are_rejected(self, world, org_a):
        other = services.create_checklist_template(organization=org_a, name="Other", code="OTH-2")
        other_version = other.versions.get()
        with pytest.raises(lifecycle.LifecycleError) as info:
            lifecycle.compare_versions(world["template"], world["v1"], other_version)
        assert info.value.lifecycle_code == "foreign_version" and "same checklist template" in str(info.value)

    def test_the_comparison_is_audited_once_per_request(self, world):
        lifecycle.compare_versions(world["template"], world["v1"], world["v1"], actor=world["author"])
        event = ActivityLog.objects.get(verb="checklist_version.compared")
        assert event.metadata["from"] == 1 and event.metadata["to"] == 1 and event.metadata["changes"] == 0
        lifecycle.compare_versions(world["template"], world["v1"], world["v1"], actor=world["author"], log=False)
        assert ActivityLog.objects.filter(verb="checklist_version.compared").count() == 1

    def test_a_draft_can_be_compared_with_its_base(self, world):
        author = world["author"]
        v2 = lifecycle.create_draft(world["template"], actor=author)
        services.update_item(item_of(v2, "FIRE-EXIT"), actor=author, is_required=False)
        result = lifecycle.compare_versions(world["template"], world["v1"], v2, log=False)
        assert [c.field for c in result.changes] == ["is_required"]

    def test_query_count_is_flat(self, world, django_assert_max_num_queries):
        v2, _ = self.evolve(world)
        with django_assert_max_num_queries(16):
            lifecycle.compare_versions(world["template"], world["v1"], v2, actor=world["author"])
