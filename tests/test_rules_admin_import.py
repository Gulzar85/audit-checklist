"""Rules through the other doors: the admin form, bulk import/export, and the builder's form model."""

from __future__ import annotations

import json

import pytest
from django.contrib import admin
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import RequestFactory

from apps.checklists import imports, services
from apps.checklists.admin import ChecklistRuleAdmin, RuleAdminForm
from apps.checklists.models import ChecklistRule, ChecklistVersion
from apps.checklists.rules import builder, snapshot_for_version
from apps.permissions import services as rbac
from apps.permissions.registry import P


def act(kind, target=None, target_type="item", **params):
    out = {"type": kind, **params}
    if target:
        out["target"] = {"type": target_type, "code": target}
    return out


@pytest.fixture
def world(org_a, org_b, make_user, assign):
    author = make_user(email="ri-author@example.com", organization=org_a, role=None)
    assign(author, org_a, "administrator")
    reader = make_user(email="ri-reader@example.com", organization=org_a, role=None)
    assign(reader, org_a, rbac.create_role(organization=org_a, name="Reader", permissions=[P.CHECKLIST_VIEW]))
    other = make_user(email="ri-other@example.com", organization=org_b, role=None)
    assign(other, org_b, "administrator")
    template = services.create_checklist_template(organization=org_a, name="Daily", code="DAY-1", actor=author)
    draft = template.versions.get()
    section = services.create_section(draft, name="Safety", actor=author)
    services.create_item(section, question="Fire exit ok?", code="FIRE", answer_type="yes_no", actor=author)
    services.create_item(section, question="Temperature", code="TEMP", answer_type="numeric", config={"min": -10, "max": 40, "decimals": 1}, actor=author)
    services.create_item(section, question="Notes", code="NOTE", answer_type="long_text", is_required=False, actor=author)
    services.create_item(section, question="Lane clear?", code="DT", answer_type="yes_no", actor=author)
    services.create_item(section, question="Condition", code="COND", answer_type="single_choice", options=[("good", "Good"), ("poor", "Poor")], actor=author)
    foreign = services.create_checklist_template(organization=org_b, name="Foreign", code="DAY-1", actor=other)
    fsec = services.create_section(foreign.versions.get(), name="F", actor=other)
    services.create_item(fsec, question="Secret", code="SECRET", actor=other)
    services.create_item(fsec, question="Second", code="SECOND", actor=other)
    return {"author": author, "reader": reader, "other": other, "template": template, "draft": draft, "foreign": foreign}


ROW = {"template": "day-1", "name": "Blocked exit", "enabled": "true",
       "condition": json.dumps({"source_item_code": "FIRE", "comparison": "EQUALS", "value": "no"}),
       "actions": json.dumps([act("SHOW_ITEM", "NOTE")])}


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestImport:
    def test_creates_rules_through_the_service(self, world, org_a):
        rows = [ROW, {**ROW, "name": "Second", "code": "r-custom", "condition": "", "actions": json.dumps([act("DISPLAY_MESSAGE", message="hi")]), "enabled": "no"}]
        assert imports.import_rules(user=world["author"], organization=org_a, rows=rows) == {"rules": 2, "templates": 1}
        first, second = world["draft"].rules.order_by("sort_order")
        assert (first.code, first.is_active, second.code, second.is_active, second.condition) == ("R-001", True, "R-CUSTOM", False, None)

    def test_row_level_errors_and_all_or_nothing(self, world, org_a):
        rows = [ROW,
                {**ROW, "name": "bad op", "condition": json.dumps({"source_item_code": "FIRE", "comparison": "GREATER_THAN", "value": 3})},
                {**ROW, "name": "", "actions": ""},
                {**ROW, "name": "bad json", "actions": "{nope"},
                {**ROW, "name": "unknown", "template": "nope"}]
        with pytest.raises(ValidationError) as caught:
            imports.import_rules(user=world["author"], organization=org_a, rows=rows)
        text = " ".join(caught.value.messages)
        assert "Row 3:" in text and "not available" in text and "Row 4:" in text and "Missing" in text
        assert "Row 5:" in text and "valid JSON" in text and "Row 6:" in text and "not found" in text
        assert not ChecklistRule.objects.filter(version=world["draft"]).exists()  # the good first row rolled back too

    def test_a_cycle_across_rows_is_rejected(self, world, org_a):
        rows = [{**ROW, "name": "a", "actions": json.dumps([act("SHOW_ITEM", "DT")])},
                {**ROW, "name": "b", "condition": json.dumps({"source_item_code": "DT", "comparison": "EQUALS", "value": "yes"}),
                 "actions": json.dumps([act("SHOW_ITEM", "FIRE")])}]
        with pytest.raises(ValidationError) as caught:
            imports.import_rules(user=world["author"], organization=org_a, rows=rows)
        assert "Row 3:" in " ".join(caught.value.messages) and "circle" in " ".join(caught.value.messages)
        assert not ChecklistRule.objects.filter(version=world["draft"]).exists()

    def test_cannot_import_without_edit_rights_or_across_tenants(self, world, org_a, org_b):
        with pytest.raises(PermissionDenied):
            imports.import_rules(user=world["reader"], organization=org_a, rows=[ROW])
        with pytest.raises(PermissionDenied):
            imports.import_rules(user=world["author"], organization=org_b, rows=[ROW])
        with pytest.raises(PermissionDenied):
            imports.import_rules(user=world["other"], organization=org_a, rows=[ROW])
        assert not ChecklistRule.objects.filter(version=world["draft"]).exists()

    def test_other_tenants_template_code_is_indistinguishable_from_unknown(self, world, org_b):
        # org_b has its own DAY-1: importing into org_b as the org_b admin targets *its* draft, never org_a's.
        row = {**ROW, "condition": json.dumps({"source_item_code": "SECRET", "comparison": "EQUALS", "value": "yes"}),
               "actions": json.dumps([act("SHOW_ITEM", "SECOND")])}
        assert imports.import_rules(user=world["other"], organization=org_b, rows=[row])["rules"] == 1
        assert not ChecklistRule.objects.filter(version=world["draft"]).exists()
        with pytest.raises(ValidationError):  # and org_a's question codes do not exist there
            imports.import_rules(user=world["other"], organization=org_b, rows=[ROW])

    def test_published_templates_and_oversized_cells(self, world, org_a):
        with pytest.raises(ValidationError, match="at most"):
            imports.import_rules(user=world["author"], organization=org_a, rows=[{**ROW, "actions": "[" * 30_000}])
        services.publish_checklist_version(world["draft"], actor=world["author"])
        with pytest.raises(ValidationError, match="not found"):
            imports.import_rules(user=world["author"], organization=org_a, rows=[ROW])

    def test_export_round_trips(self, world, org_a):
        imports.import_rules(user=world["author"], organization=org_a, rows=[ROW, {**ROW, "name": "Two", "condition": "", "actions": json.dumps([act("DISPLAY_MESSAGE", message="x")])}])
        rows = imports.export_rules(user=world["author"], template=world["template"])
        assert [r["code"] for r in rows] == ["R-001", "R-002"] and rows[1]["condition"] == ""
        # Import the export into a copy of the template: the same rules come out.
        copy = services.duplicate_checklist_template(world["template"], actor=world["author"], name="Copy", code="DAY-COPY")
        services.delete_rule(copy.versions.get().rules.first(), actor=world["author"]) if copy.versions.get().rules.exists() else None
        for r in list(copy.versions.get().rules.all()):
            services.delete_rule(r, actor=world["author"])
        again = imports.import_rules(user=world["author"], organization=org_a, rows=[{**r, "template": "DAY-COPY"} for r in rows])
        assert again["rules"] == 2
        assert [r["name"] for r in imports.export_rules(user=world["author"], template=copy)] == ["Blocked exit", "Two"]

    def test_export_is_authorized(self, world):
        with pytest.raises(ValidationError):
            imports.export_rules(user=world["other"], template=world["template"])
        assert imports.export_rules(user=world["reader"], template=world["template"]) == []  # viewing is enough


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestAdmin:
    def data(self, world, **overrides):
        data = {"version": world["draft"].pk, "code": "R-ADM", "name": "Admin rule", "description": "", "is_active": True, "sort_order": 1,
                "condition": {"source_item_code": "FIRE", "comparison": "equals", "value": "no"}.copy(), "actions": [act("SHOW_ITEM", "NOTE")]}
        data["condition"]["comparison"] = "EQUALS"
        data.update(overrides)
        return data

    def form(self, world, instance=None, **overrides):
        payload = self.data(world, **overrides)
        payload["condition"], payload["actions"] = json.dumps(payload["condition"]), json.dumps(payload["actions"])
        return RuleAdminForm(payload, instance=instance)

    def test_valid_rule_is_saved_in_normalized_form(self, world):
        form = self.form(world, condition={"source_item_code": "TEMP", "comparison": "GREATER_THAN", "value": 5})
        assert form.is_valid(), form.errors
        assert form.cleaned_data["condition"] == {"operator": "AND", "conditions": [{"source_item_code": "TEMP", "comparison": "GREATER_THAN", "value": "5.0"}]}

    @pytest.mark.parametrize("override", [
        {"condition": {"source_item_code": "NOPE", "comparison": "EQUALS", "value": "x"}},
        {"condition": {"source_item_code": "FIRE", "comparison": "GREATER_THAN", "value": 3}},
        {"actions": [act("SHOW_ITEM", "NOPE")]},
        {"actions": [act("EXEC", "NOTE")]},
        {"actions": []},
    ])
    def test_invalid_rules_are_refused(self, world, override):
        form = self.form(world, **override)
        assert not form.is_valid() and (form.non_field_errors() or form.errors)

    def test_cycles_are_refused(self, world):
        services.create_rule(world["draft"], name="a", condition={"source_item_code": "FIRE", "comparison": "EQUALS", "value": "no"}, actions=[act("SHOW_ITEM", "DT")], actor=world["author"])
        form = self.form(world, condition={"source_item_code": "DT", "comparison": "EQUALS", "value": "yes"}, actions=[act("SHOW_ITEM", "FIRE")])
        assert not form.is_valid() and "circle" in " ".join(form.non_field_errors())

    def test_published_versions_are_frozen_and_only_drafts_are_offered(self, world, rf=RequestFactory()):
        rule = services.create_rule(world["draft"], name="a", condition=None, actions=[act("DISPLAY_MESSAGE", message="m")], actor=world["author"])
        services.publish_checklist_version(world["draft"], actor=world["author"])
        model_admin = ChecklistRuleAdmin(ChecklistRule, admin.site)
        request = rf.get("/")
        request.user = type("U", (), {"has_perm": lambda self, p: True, "is_active": True, "is_staff": True})()
        rule.refresh_from_db()
        assert not model_admin.has_change_permission(request, rule) and not model_admin.has_delete_permission(request, rule)
        assert model_admin.get_readonly_fields(request, rule) == ("version", "code")
        field = model_admin.formfield_for_foreignkey(ChecklistRule._meta.get_field("version"), request)
        assert not field.queryset.filter(pk=world["draft"].pk).exists() and field.queryset.model is ChecklistVersion
        form = self.form(world)  # a published version never accepts a new rule
        assert not form.is_valid() and "draft" in " ".join(form.non_field_errors())


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestBuilderModel:
    def snapshot(self, world):
        return snapshot_for_version(world["draft"])

    def test_parse_post_is_bounded_and_compacts_indices(self, world, settings):
        settings.CHECKLIST_RULE_LIMITS = {"max_conditions_per_rule": 3, "max_actions_per_rule": 2}
        post = {"name": "n" * 500, "c0_src": "FIRE", "c7_src": "TEMP", "c9_src": "NOTE", "c11_src": "DT", "c12_src": "COND",
                "a0_type": "SHOW_ITEM", "a4_type": "HIDE_ITEM", "a5_type": "MAKE_OPTIONAL", "g0_logic": "OR", "g0c0_src": "FIRE", "evil": "x"}
        state = builder.parse_post(post)
        assert len(state["name"]) == 200 and len(state["conditions"]) == 3 and len(state["actions"]) == 2
        assert [r["src"] for r in state["conditions"]] == ["FIRE", "TEMP", "NOTE"]
        assert state["groups"][0]["logic"] == "OR" and state["groups"][0]["conditions"][0]["src"] == "FIRE"

    def test_normalize_repairs_stale_choices(self, world):
        snapshot = self.snapshot(world)
        state = builder.parse_post({"c0_src": "NOTE", "c0_op": "GREATER_THAN", "c0_val": "5", "c1_src": "GONE", "c1_op": "EQUALS", "c1_val": "x",
                                    "a0_type": "NOT_AN_ACTION", "a1_type": "SHOW_SECTION", "a1_target": "item:FIRE",
                                    "a2_type": "OPTION_AVAILABILITY", "a2_target": "item:FIRE", "a3_type": "SET_VALIDATION_CONSTRAINT", "a3_target": "item:FIRE"})
        builder.normalize_state(snapshot, state)
        assert state["conditions"][0]["op"] == "EQUALS" and state["conditions"][0]["val"] == ""  # text question: ordered operators are not offered
        assert state["conditions"][1]["src"] == "" and state["conditions"][1]["op"] == ""
        assert [a["type"] for a in state["actions"]] == ["", "SHOW_SECTION", "OPTION_AVAILABILITY", "SET_VALIDATION_CONSTRAINT"]
        assert [a["target"] for a in state["actions"]] == ["", "", "", ""]  # a yes/no question cannot be restricted or tightened

    def test_pickers_only_offer_valid_targets(self, world):
        snapshot = self.snapshot(world)
        state = builder.parse_post({"a0_type": "OPTION_AVAILABILITY", "a1_type": "SET_VALIDATION_CONSTRAINT", "a2_type": "SHOW_ITEM"})
        rows = builder.action_rows(snapshot, state)
        offered = [[value for _g, choices in row["target_groups"] for value, _l in choices] for row in rows]
        assert offered[0] == ["item:COND"]
        assert set(offered[1]) == {"item:TEMP", "item:NOTE"}  # numeric and text have limits to tighten; yes/no and choice do not
        assert len(offered[2]) == 5

    def test_state_roundtrip_preserves_the_stored_rule(self, world):
        rule = services.create_rule(
            world["draft"], name="x", actor=world["author"],
            condition={"operator": "OR", "conditions": [
                {"source_item_code": "FIRE", "comparison": "EQUALS", "value": "no"},
                {"operator": "AND", "conditions": [{"source_item_code": "TEMP", "comparison": "BETWEEN", "value": ["0", "5"]},
                                                   {"source_item_code": "DT", "comparison": "EQUALS", "value": "yes"}]}]},
            actions=[act("REQUIRE_EVIDENCE", "NOTE", kinds=["photo", "file"], min_count=2), act("OPTION_AVAILABILITY", "COND", mode="exclude", options=["poor"]),
                     act("SET_VALIDATION_CONSTRAINT", "NOTE", constraints={"max_length": 100}), act("DISPLAY_MESSAGE", severity="error", message="m")],
        )
        snapshot = self.snapshot(world)
        state = builder.state_from_rule(rule.name, rule.description, rule.condition, rule.actions)
        post = {"name": "x", "logic": state["logic"], "json_only": "0"}
        for i, row in enumerate(state["conditions"]):
            post.update({f"c{i}_src": row["src"], f"c{i}_op": row["op"], f"c{i}_val": row["val"], f"c{i}_val2": row["val2"]})
        for j, group in enumerate(state["groups"]):
            post[f"g{j}_logic"] = group["logic"]
            for k, row in enumerate(group["conditions"]):
                post.update({f"g{j}c{k}_src": row["src"], f"g{j}c{k}_op": row["op"], f"g{j}c{k}_val": row["val"], f"g{j}c{k}_val2": row["val2"]})
        for i, action in enumerate(state["actions"]):
            post[f"a{i}_type"], post[f"a{i}_target"] = action["type"], action["target"]
            post.update({f"a{i}_{k}": v for k, v in action["p"].items()})

        class Post(dict):
            def getlist(self, key):
                value = self.get(key, [])
                return value if isinstance(value, list) else [value]

        condition, actions, _shape = builder.build_payload(snapshot, builder.parse_post(Post(post)), strict=True)
        assert condition["operator"] == "OR" and condition["conditions"][0] == rule.condition["conditions"][0]
        assert condition == rule.condition  # nested group, range and values survive the round trip through the editor state
        assert [(a["type"], a.get("target")) for a in actions] == [(a["type"], a.get("target")) for a in rule.actions]
        assert actions[0]["kinds"] == ["photo", "file"] and actions[0]["min_count"] == 2 and actions[1]["mode"] == "exclude"
        assert actions[2]["constraints"] == {"max_length": "100"}  # typed text; the service normalizes it and actions[3]["severity"] == "error"

    def test_json_panel_rejects_garbage(self):
        assert builder.parse_json_input("nope")[1] and builder.parse_json_input("[]")[1] and builder.parse_json_input('{"evil": 1}')[1]
        assert builder.parse_json_input('{"condition": NaN}')[1] and builder.parse_json_input("x" * 60_000)[1]
        assert builder.parse_json_input('{"condition": null, "actions": []}') == ({"condition": None, "actions": []}, "")
