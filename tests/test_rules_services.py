"""Rule services against the database: validation gate, audit trail, version safety, structure guards, snapshots."""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError

from apps.checklists import selectors, services
from apps.checklists.models import ChecklistRule
from apps.checklists.rules import evaluate_snapshot, snapshot_for_version, validate_rule_set
from apps.checklists.services import RuleValidationError
from apps.core.models import ActivityLog


def cond(source, comparison, value=None):
    out = {"source_item_code": source, "comparison": comparison}
    if value is not None:
        out["value"] = value
    return out


def act(kind, target=None, target_type="item", **params):
    out = {"type": kind, **params}
    if target:
        out["target"] = {"type": target_type, "code": target}
    return out


@pytest.fixture
def author(make_user, assign, org_a):
    user = make_user(email="rules-author@example.com", organization=org_a, role=None)
    assign(user, org_a, "administrator")
    return user


@pytest.fixture
def world(org_a, author):
    template = services.create_checklist_template(organization=org_a, name="Daily", code="DAY-1", actor=author)
    draft = template.versions.get()
    s1 = services.create_section(draft, name="Safety", actor=author)
    s2 = services.create_section(draft, name="Drive-through", actor=author)
    items = {
        "FIRE": services.create_item(s1, question="Is the fire exit accessible?", code="FIRE", answer_type="yes_no", actor=author),
        "TEMP": services.create_item(s1, question="Record refrigerator temperature", code="TEMP", answer_type="numeric",
                                     config={"min": -10, "max": 40, "decimals": 1, "unit": "°C"}, actor=author),
        "NOTE": services.create_item(s1, question="Describe the issue", code="NOTE", answer_type="long_text", is_required=False, actor=author),
        "COND": services.create_item(s1, question="Condition", code="COND", answer_type="single_choice",
                                     options=[("good", "Good"), ("damaged", "Damaged")], actor=author),
        "DT": services.create_item(s2, question="Drive-through lane clear?", code="DT", answer_type="yes_no", actor=author),
    }
    return {"template": template, "draft": draft, "s1": s1, "s2": s2, "items": items, "author": author}


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestRuleCrud:
    def test_create_normalizes_and_audits(self, world):
        rule = services.create_rule(
            world["draft"], name="  Follow up on a blocked exit ", actor=world["author"],
            condition={"operator": "and", "conditions": [{"source_item_code": "FIRE", "comparison": "equals", "value": "NO"}]},
            actions=[{"type": "show_item", "target": {"type": "ITEM", "code": "NOTE"}}],
        )
        assert rule.code == "R-001" and rule.name == "Follow up on a blocked exit" and rule.sort_order == 1
        assert rule.condition == {"operator": "AND", "conditions": [{"source_item_code": "FIRE", "comparison": "EQUALS", "value": "no"}]}
        assert rule.actions == [{"type": "SHOW_ITEM", "target": {"type": "item", "code": "NOTE"}}]
        event = ActivityLog.objects.get(verb="checklist_rule.created")
        assert event.actor == world["author"] and event.metadata["template_id"] == str(world["template"].pk)
        assert rule.history.count() == 1
        assert services.create_rule(world["draft"], name="Second", actions=[act("DISPLAY_MESSAGE", message="x")], actor=world["author"]).code == "R-002"

    def test_update_logs_condition_and_action_changes(self, world):
        rule = services.create_rule(world["draft"], name="R", condition=cond("FIRE", "EQUALS", "no"), actions=[act("REQUIRE_COMMENT", "FIRE")], actor=world["author"])
        services.update_rule(rule, condition=cond("FIRE", "EQUALS", "yes"), actor=world["author"])
        services.update_rule(rule, actions=[act("DISPLAY_MESSAGE", "FIRE", severity="warning", message="Hmm")], name="Renamed", actor=world["author"])
        verbs = list(ActivityLog.objects.filter(verb__startswith="checklist_rule.").values_list("verb", flat=True))
        assert "checklist_rule.condition_changed" in verbs and "checklist_rule.actions_changed" in verbs
        assert verbs.count("checklist_rule.updated") == 2
        rule.refresh_from_db()
        assert rule.name == "Renamed" and rule.code == "R-001" and rule.history.count() == 3
        before = ActivityLog.objects.count()
        services.update_rule(rule, name="Renamed", actor=world["author"])  # no change: nothing logged
        assert ActivityLog.objects.count() == before

    def test_disable_enable_and_delete(self, world):
        rule = services.create_rule(world["draft"], name="R", actions=[act("DISPLAY_MESSAGE", message="x")], actor=world["author"])
        services.set_rule_enabled(rule, False, actor=world["author"])
        rule.refresh_from_db()
        assert rule.is_active is False
        assert evaluate_snapshot(snapshot_for_version(world["draft"]), {}).messages == []
        services.set_rule_enabled(rule, True, actor=world["author"])
        assert evaluate_snapshot(snapshot_for_version(world["draft"]), {}).messages[0]["message"] == "x"
        second = services.create_rule(world["draft"], name="S", actions=[act("DISPLAY_MESSAGE", message="y")], actor=world["author"])
        services.delete_rule(rule, actor=world["author"])
        second.refresh_from_db()
        assert second.sort_order == 1  # the gap closed
        assert {"checklist_rule.disabled", "checklist_rule.enabled", "checklist_rule.removed"} <= set(ActivityLog.objects.values_list("verb", flat=True))

    def test_reorder_and_move(self, world):
        rules = [services.create_rule(world["draft"], name=f"R{i}", actions=[act("DISPLAY_MESSAGE", message=f"m{i}")], actor=world["author"]) for i in range(3)]
        services.move_rule(rules[2], "up", actor=world["author"])
        assert list(world["draft"].rules.order_by("sort_order").values_list("name", flat=True)) == ["R0", "R2", "R1"]
        services.reorder_rules(world["draft"], [rules[1].pk, rules[0].pk, rules[2].pk], actor=world["author"])
        assert [m["message"] for m in evaluate_snapshot(snapshot_for_version(world["draft"]), {}).messages] == ["m1", "m0", "m2"]
        with pytest.raises(ValidationError):
            services.reorder_rules(world["draft"], [rules[0].pk], actor=world["author"])

    def test_name_and_code_rules(self, world):
        for kwargs in ({"name": ""}, {"name": "x" * 121}, {"name": "ok", "description": "x" * 301}, {"name": "ok", "code": "bad code!"}):
            with pytest.raises(ValidationError):
                services.create_rule(world["draft"], actions=[act("DISPLAY_MESSAGE", message="x")], actor=world["author"], **kwargs)
        services.create_rule(world["draft"], name="a", code="my-rule", actions=[act("DISPLAY_MESSAGE", message="x")], actor=world["author"])
        with pytest.raises(ValidationError):
            services.create_rule(world["draft"], name="b", code="MY-RULE", actions=[act("DISPLAY_MESSAGE", message="x")], actor=world["author"])

    def test_code_is_permanent(self, world):
        rule = services.create_rule(world["draft"], name="R", actions=[act("DISPLAY_MESSAGE", message="x")], actor=world["author"])
        services.update_rule(rule, code="HACK", actor=world["author"])
        rule.refresh_from_db()
        assert rule.code == "R-001"


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestValidationGate:
    def test_invalid_rule_is_refused_with_all_issues(self, world):
        with pytest.raises(RuleValidationError) as info:
            services.create_rule(
                world["draft"], name="Bad", actor=world["author"],
                condition={"operator": "AND", "conditions": [cond("NOPE", "EQUALS", "x"), cond("FIRE", "GREATER_THAN", 3)]},
                actions=[act("SHOW_ITEM", "ALSO_NOPE")],
            )
        assert len(info.value.issues) >= 3
        assert {"condition", "actions"} <= set(info.value.message_dict)
        assert not ChecklistRule.objects.exists()

    def test_validation_failure_is_audited_without_user_text(self, world):
        with pytest.raises(RuleValidationError):
            services.create_rule(world["draft"], name="Bad", actions=[act("DISPLAY_MESSAGE", message="SECRET-TEXT", severity="fatal")], actor=world["author"])
        event = ActivityLog.objects.get(verb="checklist_rule.validation_failed")
        assert event.metadata["count"] >= 1 and "SECRET-TEXT" not in str(event.metadata) and "SECRET-TEXT" not in event.description

    def test_cycles_are_rejected_when_the_second_rule_is_added(self, world):
        services.create_rule(world["draft"], name="A shows B", condition=cond("FIRE", "EQUALS", "no"), actions=[act("SHOW_ITEM", "DT")], actor=world["author"])
        with pytest.raises(RuleValidationError) as info:
            services.create_rule(world["draft"], name="B shows A", condition=cond("DT", "EQUALS", "yes"), actions=[act("SHOW_ITEM", "FIRE")], actor=world["author"])
        assert info.value.issues[0].code == "cycle" and "R-001" in str(info.value.message_dict)
        assert ChecklistRule.objects.count() == 1

    def test_editing_a_rule_into_a_cycle_is_rejected(self, world):
        services.create_rule(world["draft"], name="1", condition=cond("FIRE", "EQUALS", "no"), actions=[act("SHOW_ITEM", "NOTE")], actor=world["author"])
        second = services.create_rule(world["draft"], name="2", condition=cond("NOTE", "IS_NOT_EMPTY"), actions=[act("SHOW_ITEM", "DT")], actor=world["author"])
        with pytest.raises(RuleValidationError):
            services.update_rule(second, actions=[act("SHOW_ITEM", "FIRE")], actor=world["author"])
        second.refresh_from_db()
        assert second.actions[0]["target"]["code"] == "DT"

    def test_enabling_revalidates(self, world):
        a = services.create_rule(world["draft"], name="1", condition=cond("FIRE", "EQUALS", "no"), actions=[act("SHOW_ITEM", "DT")], actor=world["author"])
        b = services.create_rule(world["draft"], name="2", condition=cond("DT", "EQUALS", "yes"), actions=[act("SHOW_ITEM", "NOTE")], actor=world["author"])
        services.set_rule_enabled(b, False, actor=world["author"])
        ChecklistRule.objects.filter(pk=b.pk).update(actions=[act("SHOW_ITEM", "FIRE")])  # a cycle hiding in a disabled rule
        b.refresh_from_db()
        with pytest.raises(RuleValidationError):
            services.set_rule_enabled(b, True, actor=world["author"])
        assert a.is_active

    def test_conflicting_identical_conditions_rejected(self, world):
        services.create_rule(world["draft"], name="show", condition=cond("FIRE", "EQUALS", "no"), actions=[act("SHOW_ITEM", "NOTE")], actor=world["author"])
        with pytest.raises(RuleValidationError) as info:
            services.create_rule(world["draft"], name="hide", condition=cond("FIRE", "EQUALS", "no"), actions=[act("HIDE_ITEM", "NOTE")], actor=world["author"])
        assert info.value.issues[0].code == "conflict"

    def test_a_rule_cannot_reference_another_versions_question(self, org_a, world):
        other = services.create_checklist_template(organization=org_a, name="Other", code="OTH-1", actor=world["author"])
        section = services.create_section(other.versions.get(), name="S", actor=world["author"])
        services.create_item(section, question="Foreign", code="FOREIGN", actor=world["author"])
        with pytest.raises(RuleValidationError):
            services.create_rule(world["draft"], name="x", condition=cond("FOREIGN", "EQUALS", "yes"), actions=[act("SHOW_ITEM", "NOTE")], actor=world["author"])

    def test_rule_count_limit(self, world, settings):
        settings.CHECKLIST_RULE_LIMITS = {"max_rules_per_version": 2}
        for i in range(2):
            services.create_rule(world["draft"], name=f"R{i}", actions=[act("DISPLAY_MESSAGE", message="x")], actor=world["author"])
        with pytest.raises(RuleValidationError):
            services.create_rule(world["draft"], name="third", actions=[act("DISPLAY_MESSAGE", message="x")], actor=world["author"])


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestVersionSafety:
    def make_rules(self, world):
        return services.create_rule(world["draft"], name="Fire", condition=cond("FIRE", "EQUALS", "no"),
                                    actions=[act("SHOW_ITEM", "NOTE"), act("REQUIRE_COMMENT", "FIRE")], actor=world["author"])

    def test_published_version_rules_are_frozen(self, world):
        rule = self.make_rules(world)
        services.publish_checklist_version(world["draft"], actor=world["author"])
        for call in (
            lambda: services.create_rule(world["draft"], name="n", actions=[act("DISPLAY_MESSAGE", message="x")]),
            lambda: services.update_rule(rule, name="changed"),
            lambda: services.set_rule_enabled(rule, False),
            lambda: services.delete_rule(rule),
            lambda: services.reorder_rules(world["draft"], [rule.pk]),
        ):
            with pytest.raises(ValidationError) as info:
                call()
            assert info.value.code == "version_immutable"
        rule.refresh_from_db()
        assert rule.name == "Fire" and rule.is_active

    def test_new_version_copies_rules_and_leaves_the_old_alone(self, world):
        rule = self.make_rules(world)
        services.publish_checklist_version(world["draft"], actor=world["author"])
        v2 = services.create_checklist_version(world["template"], actor=world["author"])
        copied = v2.rules.get()
        assert copied.pk != rule.pk and (copied.code, copied.condition, copied.actions) == (rule.code, rule.condition, rule.actions)
        assert validate_rule_set(snapshot_for_version(v2)).ok  # codes are stable, so the references still resolve
        services.update_rule(copied, actions=[act("DISPLAY_MESSAGE", "FIRE", message="v2 only")], actor=world["author"])
        rule.refresh_from_db()
        assert rule.actions[0]["type"] == "SHOW_ITEM"  # version 1 behaves exactly as before
        v1 = evaluate_snapshot(snapshot_for_version(world["draft"]), {"FIRE": "no"})
        assert v1.items["NOTE"].visible is True
        v2_eval = evaluate_snapshot(snapshot_for_version(v2), {"FIRE": "no"})
        assert v2_eval.items["NOTE"].visible is True  # v2 has no SHOW rule for NOTE any more: visible by default
        assert [m["message"] for m in v2_eval.messages] == ["v2 only"]  # and displays a message instead
        assert v1.items["NOTE"].visible is True and evaluate_snapshot(snapshot_for_version(world["draft"]), {"FIRE": "yes"}).items["NOTE"].visible is False

    def test_publish_is_blocked_by_invalid_rules(self, world):
        rule = self.make_rules(world)
        ChecklistRule.objects.filter(pk=rule.pk).update(condition=cond("MISSING", "EQUALS", "x"))  # corrupt it behind the services
        with pytest.raises(ValidationError) as info:
            services.publish_checklist_version(world["draft"], actor=world["author"])
        assert "Rule R-001" in " ".join(info.value.messages)
        world["draft"].refresh_from_db()
        assert world["draft"].is_draft
        assert ActivityLog.objects.filter(verb="checklist_rule.validation_failed").exists()

    def test_duplicate_template_keeps_rules(self, world):
        self.make_rules(world)
        copy = services.duplicate_checklist_template(world["template"], actor=world["author"])
        assert copy.versions.get().rules.count() == 1

    def test_structure_changes_that_would_break_rules_are_refused(self, world):
        self.make_rules(world)
        items = world["items"]
        with pytest.raises(ValidationError) as info:
            services.update_item(items["FIRE"], code="FIRE-2", actor=world["author"])
        assert "break the rules" in " ".join(info.value.messages)
        with pytest.raises(ValidationError):
            services.delete_item(items["NOTE"], actor=world["author"])
        with pytest.raises(ValidationError):
            services.update_item(items["FIRE"], answer_type="numeric", actor=world["author"])  # EQUALS "no" no longer parses
        with pytest.raises(ValidationError):
            services.delete_section(world["s1"], actor=world["author"])
        items["FIRE"].refresh_from_db()
        assert items["FIRE"].code == "FIRE" and items["FIRE"].answer_type == "yes_no"
        # Unrelated edits are fine.
        services.update_item(items["FIRE"], question="Is the fire exit clear?", actor=world["author"])
        services.update_item(items["DT"], code="DT-2", actor=world["author"])

    def test_option_changes_that_break_a_rule_are_refused(self, world):
        services.create_rule(world["draft"], name="Damaged", condition=cond("COND", "EQUALS", "damaged"), actions=[act("REQUIRE_ITEM", "NOTE")], actor=world["author"])
        option = world["items"]["COND"].options.get(value="damaged")
        with pytest.raises(ValidationError):
            services.delete_option(option, actor=world["author"])
        with pytest.raises(ValidationError):
            services.set_options(world["items"]["COND"], [("good", "Good"), ("fair", "Fair")], actor=world["author"])
        services.update_option(world["items"]["COND"].options.get(value="good"), label="Fine", actor=world["author"])  # a label change is harmless
        services.set_options(world["items"]["COND"], [("good", "Good"), ("damaged", "Damaged"), ("fair", "Fair")], actor=world["author"])

    def test_sections_get_stable_codes(self, world):
        assert [s.code for s in world["draft"].sections.order_by("sort_order")] == ["S-001", "S-002"]
        extra = services.create_section(world["draft"], name="Extra", code="drive", actor=world["author"])
        assert extra.code == "DRIVE"
        with pytest.raises(ValidationError):
            services.create_section(world["draft"], name="Dup", code="DRIVE", actor=world["author"])
        services.create_item(extra, question="Q", actor=world["author"])
        services.publish_checklist_version(world["draft"], actor=world["author"])
        v2 = services.create_checklist_version(world["template"], actor=world["author"])
        assert [s.code for s in v2.sections.order_by("sort_order")] == ["S-001", "S-002", "DRIVE"]  # codes survive a new version


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestSnapshot:
    def test_snapshot_is_loaded_in_a_constant_number_of_queries(self, world, django_assert_max_num_queries):
        for i in range(8):
            services.create_rule(world["draft"], name=f"R{i}", condition=cond("FIRE", "EQUALS", "no"), actions=[act("DISPLAY_MESSAGE", message=f"m{i}")], actor=world["author"])
        version = type(world["draft"]).objects.select_related("template").get(pk=world["draft"].pk)
        with django_assert_max_num_queries(6):
            snapshot = snapshot_for_version(version)
        assert len(snapshot.rules) == 8 and set(snapshot.items) == {"FIRE", "TEMP", "NOTE", "COND", "DT"}
        assert set(snapshot.sections) == {"S-001", "S-002"}
        with django_assert_max_num_queries(0):  # evaluation itself never touches the database
            evaluate_snapshot(snapshot, {"FIRE": "no", "TEMP": "4.5"})

    def test_snapshot_carries_normalized_answer_type_config(self, world):
        snapshot = snapshot_for_version(world["draft"])
        assert snapshot.items["TEMP"].config == {"min": -10, "max": 40, "decimals": 1, "unit": "°C"}
        assert snapshot.items["COND"].option_values() == ["good", "damaged"]
        assert snapshot.organization_id == str(world["template"].organization_id)

    def test_selectors(self, world):
        rule = services.create_rule(world["draft"], name="R", actions=[act("DISPLAY_MESSAGE", message="x")], actor=world["author"])
        assert selectors.get_rule(world["draft"], rule.pk) == rule and selectors.get_rule(world["draft"], "nope") is None
        assert list(selectors.rules_for_version(world["draft"])) == [rule]
