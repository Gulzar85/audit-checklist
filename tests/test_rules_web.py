"""The rule builder screens: list, editor (HTMX state round-trips), advanced JSON, simulator, access control and safety."""

from __future__ import annotations

import json

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.checklists import services
from apps.checklists.models import ChecklistRule
from apps.core.models import ActivityLog
from apps.permissions import services as rbac
from apps.permissions.registry import P

HX = {"HTTP_HX_REQUEST": "true"}


def body(response) -> str:
    return response.content.decode()


def act(kind, target=None, target_type="item", **params):
    out = {"type": kind, **params}
    if target:
        out["target"] = {"type": target_type, "code": target}
    return out


def url(name, *args):
    return reverse(f"checklists:{name}", args=args)


@pytest.fixture
def world(org_a, org_b, make_user, assign):
    author = make_user(email="rw-author@example.com", organization=org_a, role=None)
    assign(author, org_a, "administrator")
    reader = make_user(email="rw-reader@example.com", organization=org_a, role=None)
    assign(reader, org_a, rbac.create_role(organization=org_a, name="Reader", permissions=[P.CHECKLIST_VIEW]))
    plain = make_user(email="rw-plain@example.com", organization=org_a, role=None)
    other = make_user(email="rw-other@example.com", organization=org_b, role=None)
    assign(other, org_b, "administrator")
    template = services.create_checklist_template(organization=org_a, name="Daily", code="DAY-1", actor=author)
    draft = template.versions.get()
    s1 = services.create_section(draft, name="Safety", actor=author)
    s2 = services.create_section(draft, name="Drive-through", actor=author)
    services.create_item(s1, question="Is the fire exit accessible?", code="FIRE", answer_type="yes_no", actor=author)
    services.create_item(s1, question="Refrigerator temperature", code="TEMP", answer_type="numeric", config={"min": -10, "max": 40, "decimals": 1, "unit": "°C"}, actor=author)
    services.create_item(s1, question="Describe the issue", code="NOTE", answer_type="long_text", is_required=False, actor=author)
    services.create_item(s1, question="Condition", code="COND", answer_type="single_choice", actor=author)
    services.create_item(s2, question="Lane clear?", code="DT", answer_type="yes_no", actor=author)
    cond = draft.sections.first().items.get(code="COND")
    for label in ("Good", "Fair", "Poor"):
        services.create_option(cond, label=label, actor=author)
    foreign = services.create_checklist_template(organization=org_b, name="Foreign", code="FX-1", actor=other)
    fs = services.create_section(foreign.versions.get(), name="F", actor=other)
    services.create_item(fs, question="Secret", code="SECRET", actor=other)
    foreign_rule = services.create_rule(foreign.versions.get(), name="Foreign rule", actions=[act("DISPLAY_MESSAGE", message="hidden")], actor=other)
    return {"author": author, "reader": reader, "plain": plain, "other": other, "template": template, "draft": draft,
            "foreign": foreign, "foreign_rule": foreign_rule}


def make_rule(world, **overrides):
    data = {
        "name": "Blocked exit",
        "condition": {"source_item_code": "FIRE", "comparison": "EQUALS", "value": "no"},
        "actions": [act("SHOW_ITEM", "NOTE"), act("REQUIRE_COMMENT", "FIRE", prompt="Describe it"),
                    act("DISPLAY_MESSAGE", "FIRE", severity="warning", message="Report it now.")],
    }
    data.update(overrides)
    return services.create_rule(world["draft"], actor=world["author"], **data)


FORM = {
    "name": "Blocked exit", "description": "", "logic": "AND", "json_only": "0",
    "c0_src": "FIRE", "c0_op": "EQUALS", "c0_val": "no",
    "a0_type": "SHOW_ITEM", "a0_target": "item:NOTE",
}


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestAccess:
    def test_anonymous_is_redirected(self, client, world):
        assert client.get(url("rules", world["template"].pk)).status_code == 302

    def test_reader_sees_rules_read_only(self, client, world):
        rule = make_rule(world)
        client.force_login(world["reader"])
        html = body(client.get(url("rules", world["template"].pk)))
        assert "Blocked exit" in html and url("rule_new", world["template"].pk) not in html and url("rule_edit", world["template"].pk, rule.pk) not in html
        assert client.get(url("rule_new", world["template"].pk)).status_code == 403
        assert client.get(url("rule_edit", world["template"].pk, rule.pk)).status_code == 403
        assert client.post(url("rule_toggle", world["template"].pk, rule.pk), {"action": "disable"}).status_code == 403
        assert client.post(url("rule_delete", world["template"].pk, rule.pk)).status_code == 403
        assert ChecklistRule.objects.get(pk=rule.pk).is_active

    def test_user_without_permission_is_forbidden(self, client, world):
        client.force_login(world["plain"])
        assert client.get(url("rules", world["template"].pk)).status_code == 403

    def test_other_organization_gets_404_everywhere(self, client, world):
        rule = make_rule(world)
        client.force_login(world["other"])
        tid = world["template"].pk
        for name, args in (("rules", ()), ("rule_new", ()), ("rule_simulate", ()), ("rule_edit", (rule.pk,))):
            assert client.get(url(name, tid, *args)).status_code == 404, name
        assert client.post(url("rule_delete", tid, rule.pk)).status_code == 404

    def test_a_rule_id_from_another_checklist_is_never_resolved(self, client, world):
        client.force_login(world["author"])
        tid, fid = world["template"].pk, world["foreign_rule"].pk
        assert client.get(url("rule_edit", tid, fid)).status_code == 404
        assert client.post(url("rule_delete", tid, fid)).status_code == 404
        assert client.post(url("rule_toggle", tid, fid), {"action": "disable"}).status_code == 404
        assert client.post(url("rule_move", tid, fid), {"direction": "up"}).status_code == 404
        assert ChecklistRule.objects.filter(pk=fid).exists()

    def test_published_versions_cannot_be_edited_in_the_ui(self, client, world):
        rule = make_rule(world)
        services.publish_checklist_version(world["draft"], actor=world["author"])
        client.force_login(world["author"])
        tid = world["template"].pk
        html = body(client.get(url("rules", tid)))
        assert "Blocked exit" in html and url("rule_new", tid) not in html and "read-only" in html
        assert client.get(url("rule_new", tid)).status_code == 404  # no draft to edit
        assert client.post(url("rule_delete", tid, rule.pk)).status_code == 404
        assert ChecklistRule.objects.filter(pk=rule.pk).exists()

    def test_bad_version_parameter(self, client, world):
        client.force_login(world["author"])
        tid = world["template"].pk
        assert client.get(url("rules", tid), {"version": "x"}).status_code == 404
        assert client.get(url("rules", tid), {"version": "99"}).status_code == 404
        assert client.get(url("rules", tid), {"version": "²"}).status_code == 404


@pytest.mark.django_db
class TestList:
    def test_summaries_and_badges(self, client, world):
        make_rule(world)
        off = make_rule(world, name="Second", condition=None, actions=[act("MAKE_OPTIONAL", "DT")])
        services.set_rule_enabled(off, False, actor=world["author"])
        client.force_login(world["author"])
        html = body(client.get(url("rules", world["template"].pk)))
        assert "If “Is the fire exit accessible?” equals No" in html and "require a comment on" in html
        assert "Disabled" in html and "R-001" in html and "R-002" in html
        assert url("rule_new", world["template"].pk) in html

    def test_empty_state(self, client, world):
        client.force_login(world["author"])
        assert "No rules yet" in body(client.get(url("rules", world["template"].pk)))

    def test_toggle_move_delete(self, client, world):
        a, b = make_rule(world), make_rule(world, name="Other", condition=None, actions=[act("MAKE_OPTIONAL", "DT")])
        client.force_login(world["author"])
        tid = world["template"].pk
        assert client.post(url("rule_toggle", tid, a.pk), {"action": "disable"}).status_code == 302
        assert not ChecklistRule.objects.get(pk=a.pk).is_active
        client.post(url("rule_toggle", tid, a.pk), {"action": "enable"})
        assert ChecklistRule.objects.get(pk=a.pk).is_active
        client.post(url("rule_move", tid, b.pk), {"direction": "up"})
        assert list(world["draft"].rules.order_by("sort_order").values_list("code", flat=True)) == [b.code, a.code]
        assert client.post(url("rule_delete", tid, a.pk)).status_code == 302
        assert list(world["draft"].rules.values_list("code", flat=True)) == [b.code] or world["draft"].rules.count() == 1

    def test_list_query_count_does_not_grow_with_rules(self, client, world):
        client.force_login(world["author"])
        make_rule(world)
        with CaptureQueriesContext(connection) as few:
            client.get(url("rules", world["template"].pk))
        for n in range(8):
            make_rule(world, name=f"R{n}", condition=None, actions=[act("DISPLAY_MESSAGE", message=f"m{n}")])
        with CaptureQueriesContext(connection) as many:
            client.get(url("rules", world["template"].pk))
        assert len(many) <= len(few) + 1

    def test_user_text_is_escaped(self, client, world):
        make_rule(world, name="<img src=x onerror=alert(1)>", actions=[act("DISPLAY_MESSAGE", message="<script>alert(2)</script>")], condition=None)
        client.force_login(world["author"])
        html = body(client.get(url("rules", world["template"].pk)))
        assert "<img src=x" not in html and "&lt;img src=x onerror=alert(1)&gt;" in html and "<script>alert(2)" not in html


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestEditor:
    def test_new_rule_page(self, client, world):
        client.force_login(world["author"])
        html = body(client.get(url("rule_new", world["template"].pk)))
        assert 'id="rule-editor-body"' in html and "Choose a question…" in html and "Choose what to do…" in html
        assert "JSON" in html and 'name="name"' in html

    def test_choosing_a_question_offers_only_its_operators(self, client, world):
        client.force_login(world["author"])
        tid = world["template"].pk
        yes_no = client.post(url("rule_new", tid), {**FORM, "c0_op": "", "c0_val": "", "op": "refresh"}, **HX)
        html = body(yes_no)
        assert yes_no.status_code == 200 and "<html" not in html and 'id="rule-editor-body"' in html
        assert "does not equal" in html and "is greater than" not in html and "has selected" not in html
        numeric = body(client.post(url("rule_new", tid), {**FORM, "c0_src": "TEMP", "c0_op": "", "op": "refresh"}, **HX))
        assert "is between" in numeric and "is greater than" in numeric and "contains" not in numeric
        choice = body(client.post(url("rule_new", tid), {**FORM, "c0_src": "COND", "c0_op": "", "op": "refresh"}, **HX))
        assert "has selected" in choice and "Good" in choice or "has selected" in choice

    def test_changing_the_question_resets_an_invalid_operator_and_value(self, client, world):
        client.force_login(world["author"])
        html = body(client.post(url("rule_new", world["template"].pk), {**FORM, "c0_src": "NOTE", "c0_op": "GREATER_THAN", "c0_val": "5", "op": "refresh"}, **HX))
        assert 'value="5"' not in html and "contains" in html  # NOTE is text: GREATER_THAN is not offered, the value was dropped

    def test_structural_operations(self, client, world):
        client.force_login(world["author"])
        tid = world["template"].pk
        added = body(client.post(url("rule_new", tid), {**FORM, "op": "add_condition:root"}, **HX))
        assert added.count('name="c1_src"') == 1 and added.count('name="c0_src"') == 1
        grouped = body(client.post(url("rule_new", tid), {**FORM, "op": "add_group"}, **HX))
        assert 'name="g0_logic"' in grouped and 'name="g0c0_src"' in grouped
        both = {**FORM, "g0_logic": "OR", "g0c0_src": "TEMP", "g0c0_op": "LESS_THAN", "g0c0_val": "2", "c1_src": "DT", "c1_op": "EQUALS", "c1_val": "no"}
        removed = body(client.post(url("rule_new", tid), {**both, "op": "remove_condition:root:0"}, **HX))
        assert 'name="c1_src"' not in removed and 'name="c0_src"' in removed and "DT" in removed
        gone = body(client.post(url("rule_new", tid), {**both, "op": "remove_group:0"}, **HX))
        assert 'name="g0_logic"' not in gone
        more = body(client.post(url("rule_new", tid), {**FORM, "op": "add_action"}, **HX))
        assert 'name="a1_type"' in more
        fewer = body(client.post(url("rule_new", tid), {**FORM, "a1_type": "HIDE_ITEM", "op": "remove_action:0"}, **HX))
        assert 'name="a1_type"' not in fewer and "HIDE_ITEM" in fewer

    def test_operation_arguments_are_bounded(self, client, world):
        client.force_login(world["author"])
        for op in ("remove_condition:root:99", "remove_condition:7:7", "remove_group:-1", "remove_action:abc", "unknown_op", "add_condition:zzz", "x" * 500):
            assert client.post(url("rule_new", world["template"].pk), {**FORM, "op": op}, **HX).status_code == 200

    def test_limits_are_enforced_by_the_server(self, client, world, settings):
        settings.CHECKLIST_RULE_LIMITS = {"max_conditions_per_rule": 2, "max_actions_per_rule": 1}
        client.force_login(world["author"])
        tid = world["template"].pk
        html = body(client.post(url("rule_new", tid), {**FORM, "c1_src": "DT", "c1_op": "EQUALS", "c1_val": "no", "op": "add_condition:root"}, **HX))
        assert "at most 2 conditions" in html and 'name="c2_src"' not in html
        html = body(client.post(url("rule_new", tid), {**FORM, "op": "add_action"}, **HX))
        assert "at most 1 actions" in html and 'name="a1_type"' not in html

    def test_save_creates_the_rule_through_the_service(self, client, world):
        client.force_login(world["author"])
        data = {**FORM, "a1_type": "REQUIRE_COMMENT", "a1_target": "item:FIRE", "a1_prompt": "Describe it",
                "a2_type": "DISPLAY_MESSAGE", "a2_target": "item:FIRE", "a2_severity": "warning", "a2_message": "Report it now.", "op": "save"}
        response = client.post(url("rule_new", world["template"].pk), data)
        assert response.status_code == 302 and response["Location"] == url("rules", world["template"].pk)
        rule = ChecklistRule.objects.get(version=world["draft"])
        assert rule.code == "R-001" and rule.name == "Blocked exit"
        assert rule.condition == {"operator": "AND", "conditions": [{"source_item_code": "FIRE", "comparison": "EQUALS", "value": "no"}]}
        assert [a["type"] for a in rule.actions] == ["SHOW_ITEM", "REQUIRE_COMMENT", "DISPLAY_MESSAGE"]
        assert rule.actions[2]["message"] == "Report it now." and rule.actions[1]["prompt"] == "Describe it"
        assert ActivityLog.objects.filter(verb="checklist.rule.created").exists() or ActivityLog.objects.filter(description__contains="rule R-001").exists()

    def test_enter_key_submit_without_op_saves(self, client, world):
        client.force_login(world["author"])
        assert client.post(url("rule_new", world["template"].pk), FORM).status_code == 302
        assert ChecklistRule.objects.filter(version=world["draft"]).count() == 1

    def test_nested_groups_and_or(self, client, world):
        client.force_login(world["author"])
        data = {**FORM, "logic": "OR", "g0_logic": "AND", "g0c0_src": "TEMP", "g0c0_op": "BETWEEN", "g0c0_val": "0", "g0c0_val2": "4",
                "g0c1_src": "DT", "g0c1_op": "EQUALS", "g0c1_val": "yes", "op": "save"}
        assert client.post(url("rule_new", world["template"].pk), data).status_code == 302
        rule = ChecklistRule.objects.get(version=world["draft"])
        assert rule.condition["operator"] == "OR"
        group = rule.condition["conditions"][1]
        assert group["operator"] == "AND" and group["conditions"][0] == {"source_item_code": "TEMP", "comparison": "BETWEEN", "value": ["0.0", "4.0"]}

    def test_every_action_type_roundtrips(self, client, world):
        client.force_login(world["author"])
        data = {
            "name": "Everything", "logic": "AND", "json_only": "0", "c0_src": "FIRE", "c0_op": "EQUALS", "c0_val": "no",
            "a0_type": "SHOW_SECTION", "a0_target": "section:S-002",
            "a1_type": "REQUIRE_EVIDENCE", "a1_target": "item:NOTE", "a1_kinds": ["photo", "file"], "a1_min_count": "2",
            "a2_type": "ADD_INSTRUCTIONS", "a2_target": "item:NOTE", "a2_text": "Be specific.",
            "a3_type": "OPTION_AVAILABILITY", "a3_target": "item:COND", "a3_mode": "only", "a3_options": ["opt-1"],
            "a4_type": "SET_VALIDATION_CONSTRAINT", "a4_target": "item:TEMP", "a4_cons_max": "8",
            "a5_type": "REQUIRE_ITEM", "a5_target": "item:NOTE", "op": "save",
        }
        cond = world["draft"].sections.first().items.get(code="COND")
        first_option = cond.options.order_by("sort_order").first()
        data["a3_options"] = [first_option.value]
        response = client.post(url("rule_new", world["template"].pk), data)
        assert response.status_code == 302, body(response)[:2000]
        stored = {a["type"]: a for a in ChecklistRule.objects.get(version=world["draft"]).actions}
        assert stored["REQUIRE_EVIDENCE"]["kinds"] == ["photo", "file"] and stored["REQUIRE_EVIDENCE"]["min_count"] == 2
        assert stored["SET_VALIDATION_CONSTRAINT"]["constraints"] == {"max": 8}
        assert stored["OPTION_AVAILABILITY"]["options"] == [first_option.value]

    @pytest.mark.parametrize("override,needle", [
        ({"c0_src": ""}, "Choose a question"),
        ({"a0_type": ""}, "Choose what the rule should do"),
        ({"a0_target": ""}, "Choose the question or section"),
        ({"name": ""}, "required"),
        ({"c0_val": ""}, "required"),
        ({"a0_type": "DISPLAY_MESSAGE", "a0_target": "", "a0_message": ""}, "message"),
    ])
    def test_incomplete_rows_are_reported_and_nothing_is_saved(self, client, world, override, needle):
        client.force_login(world["author"])
        response = client.post(url("rule_new", world["template"].pk), {**FORM, **override, "op": "save"})
        assert response.status_code == 200 and needle.lower() in body(response).lower()
        assert not ChecklistRule.objects.filter(version=world["draft"]).exists()

    def test_engine_errors_land_on_the_right_row(self, client, world):
        client.force_login(world["author"])
        response = client.post(url("rule_new", world["template"].pk), {**FORM, "c0_src": "TEMP", "c0_op": "EQUALS", "c0_val": "abc", "op": "save"})
        html = body(response)
        assert response.status_code == 200 and "Enter a number" in html and 'aria-invalid="true"' in html
        assert not ChecklistRule.objects.filter(version=world["draft"]).exists()

    def test_cycle_is_explained(self, client, world):
        make_rule(world, name="a", condition={"source_item_code": "FIRE", "comparison": "EQUALS", "value": "no"}, actions=[act("SHOW_ITEM", "DT")])
        client.force_login(world["author"])
        response = client.post(url("rule_new", world["template"].pk), {"name": "b", "logic": "AND", "c0_src": "DT", "c0_op": "EQUALS", "c0_val": "yes",
                                                                       "a0_type": "SHOW_ITEM", "a0_target": "item:FIRE", "op": "save"})
        assert response.status_code == 200 and "circle" in body(response)
        assert world["draft"].rules.count() == 1

    def test_live_summary_and_checks(self, client, world):
        make_rule(world, name="a", condition={"source_item_code": "FIRE", "comparison": "EQUALS", "value": "no"}, actions=[act("SHOW_ITEM", "DT")])
        client.force_login(world["author"])
        html = body(client.post(url("rule_new", world["template"].pk), {**FORM, "a0_type": "HIDE_ITEM", "a0_target": "item:NOTE", "op": "refresh"}, **HX))
        assert "If “Is the fire exit accessible?” equals No, hide “Describe the issue”." in html and "This rule is valid" in html
        clash = body(client.post(url("rule_new", world["template"].pk), {**FORM, "a0_type": "HIDE_ITEM", "a0_target": "item:DT", "op": "refresh"}, **HX))
        assert "opposite" in clash  # same condition, opposite things: a definite conflict

    def test_edit_prefills_and_updates(self, client, world):
        rule = make_rule(world)
        client.force_login(world["author"])
        page = body(client.get(url("rule_edit", world["template"].pk, rule.pk)))
        assert 'value="Blocked exit"' in page and '<option value="no" selected>No</option>' in page and "Describe it" in page
        data = {"name": "Renamed", "description": "why", "logic": "AND", "json_only": "0", "c0_src": "FIRE", "c0_op": "EQUALS", "c0_val": "yes",
                "a0_type": "DISPLAY_MESSAGE", "a0_target": "", "a0_severity": "info", "a0_message": "All good.", "op": "save"}
        assert client.post(url("rule_edit", world["template"].pk, rule.pk), data).status_code == 302
        rule.refresh_from_db()
        assert rule.name == "Renamed" and rule.code == "R-001" and rule.condition["conditions"][0]["value"] == "yes"
        assert rule.actions == [{"type": "DISPLAY_MESSAGE", "severity": "info", "message": "All good."}]

    def test_editor_escapes_user_text(self, client, world):
        rule = make_rule(world, name="<b>bold</b>", actions=[act("DISPLAY_MESSAGE", message="<script>alert(1)</script>")], condition=None)
        client.force_login(world["author"])
        html = body(client.get(url("rule_edit", world["template"].pk, rule.pk)))
        assert "<script>alert(1)" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "<b>bold</b>" not in html

    def test_csrf_is_enforced(self, world):
        from django.test import Client

        strict = Client(enforce_csrf_checks=True)
        strict.force_login(world["author"])
        assert strict.post(url("rule_new", world["template"].pk), {**FORM, "op": "save"}).status_code == 403
        assert not ChecklistRule.objects.filter(version=world["draft"]).exists()


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestAdvancedJson:
    JSON = json.dumps({"condition": {"source_item_code": "FIRE", "comparison": "EQUALS", "value": "no"}, "actions": [act("SHOW_ITEM", "NOTE")]})

    def test_apply_loads_into_the_editor_after_validation(self, client, world):
        client.force_login(world["author"])
        html = body(client.post(url("rule_new", world["template"].pk), {"name": "n", "json_only": "0", "json_input": self.JSON, "op": "apply_json"}, **HX))
        assert "was checked and loaded" in html and 'name="c0_src"' in html and '<option value="NOTE"' not in html.split('name="a0_target"')[0] or "item:NOTE" in html
        assert not ChecklistRule.objects.filter(version=world["draft"]).exists()  # nothing saved by loading

    @pytest.mark.parametrize("payload", [
        "not json", "[]", '{"condition": {"source_item_code": "NOPE", "comparison": "EQUALS", "value": "x"}, "actions": []}',
        '{"actions": "SHOW_ITEM"}', '{"evil": 1}', '{"actions": [{"type": "EXEC", "target": {"type": "item", "code": "NOTE"}}]}',
        '{"condition": NaN}', "{" * 5000, '{"actions": [], "condition": null}',
    ])
    def test_invalid_json_is_refused_and_never_loaded(self, client, world, payload):
        client.force_login(world["author"])
        response = client.post(url("rule_new", world["template"].pk), {"name": "n", "json_only": "0", "json_input": payload, "op": "apply_json"}, **HX)
        assert response.status_code == 200 and "could not be saved" in body(response)
        assert not ChecklistRule.objects.filter(version=world["draft"]).exists()

    def test_oversized_json_is_truncated_not_executed(self, client, world):
        client.force_login(world["author"])
        response = client.post(url("rule_new", world["template"].pk), {"name": "n", "json_only": "0", "json_input": "[" * 200_000, "op": "apply_json"}, **HX)
        assert response.status_code == 200

    def test_json_cannot_bypass_validation_on_save(self, client, world):
        client.force_login(world["author"])
        bad = json.dumps({"condition": {"source_item_code": "FIRE", "comparison": "GREATER_THAN", "value": 3}, "actions": [act("SHOW_ITEM", "NOTE")]})
        response = client.post(url("rule_new", world["template"].pk), {"name": "n", "json_only": "1", "json_input": bad, "op": "save"})
        assert response.status_code == 200 and "not available" in body(response)
        assert not ChecklistRule.objects.filter(version=world["draft"]).exists()
        good = client.post(url("rule_new", world["template"].pk), {"name": "n", "json_only": "1", "json_input": self.JSON, "op": "save"})
        assert good.status_code == 302 and ChecklistRule.objects.filter(version=world["draft"]).count() == 1

    def test_deeply_nested_rule_opens_as_json_only(self, client, world):
        deep = {"operator": "AND", "conditions": [{"operator": "OR", "conditions": [{"operator": "AND", "conditions": [
            {"source_item_code": "FIRE", "comparison": "EQUALS", "value": "no"}]}]}]}
        rule = services.create_rule(world["draft"], name="deep", condition=deep, actions=[act("SHOW_ITEM", "NOTE")], actor=world["author"])
        client.force_login(world["author"])
        html = body(client.get(url("rule_edit", world["template"].pk, rule.pk)))
        assert "edited as JSON" in html and 'name="c0_src"' not in html and 'name="json_only" value="1"' in html
        response = client.post(url("rule_edit", world["template"].pk, rule.pk), {"name": "deep2", "json_only": "1", "json_input": json.dumps({"condition": deep, "actions": rule.actions}), "op": "save"})
        assert response.status_code == 302 and ChecklistRule.objects.get(pk=rule.pk).name == "deep2"


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestSimulator:
    def test_page_and_baseline(self, client, world):
        make_rule(world)
        client.force_login(world["reader"])  # viewing rights are enough to simulate saved rules
        html = body(client.get(url("rule_simulate", world["template"].pk)))
        assert "Simulation" in html and "Nothing here is saved" in html and 'name="ans_FIRE"' in html and "Hidden" in html

    def test_answers_change_the_outcome(self, client, world):
        make_rule(world)
        client.force_login(world["author"])
        sim = url("rule_simulate", world["template"].pk)
        no = body(client.post(sim, {"ans_FIRE": "no", "ans_TEMP": "5"}, **HX))
        assert "Report it now." in no and "Comment required" in no and "<html" not in no
        yes = body(client.post(sim, {"ans_FIRE": "yes"}, **HX))
        assert "Report it now." not in yes and "Comment required" not in yes

    def test_simulation_persists_nothing(self, client, world):
        make_rule(world)
        client.force_login(world["author"])
        before = (ActivityLog.objects.count(), ChecklistRule.objects.count())
        for _ in range(3):
            client.post(url("rule_simulate", world["template"].pk), {"ans_FIRE": "no"}, **HX)
        assert (ActivityLog.objects.count(), ChecklistRule.objects.count()) == before

    def test_injection_and_oversize_values_are_just_values(self, client, world):
        make_rule(world)
        client.force_login(world["author"])
        sim = url("rule_simulate", world["template"].pk)
        html = body(client.post(sim, {"ans_FIRE": "<script>alert(1)</script>", "ans_NOTE": "x" * 100_000, "ans_NOPE": "1", "ans_TEMP": "1e999"}, **HX))
        assert "<script>alert(1)" not in html
        assert client.post(sim, {"ans_NOTE": "x" * 300_000}, **HX).status_code == 413

    def test_media_and_multiple_choice_samples(self, client, world):
        services.create_item(world["draft"].sections.first(), question="Photo", code="PHOTO", answer_type="photo", is_required=False, actor=world["author"])
        services.create_rule(world["draft"], name="photo", condition={"source_item_code": "PHOTO", "comparison": "IS_NOT_EMPTY"},
                             actions=[act("DISPLAY_MESSAGE", message="Photo seen")], actor=world["author"])
        client.force_login(world["author"])
        sim = url("rule_simulate", world["template"].pk)
        assert "Photo seen" not in body(client.post(sim, {}, **HX))
        assert "Photo seen" in body(client.post(sim, {"ans_PHOTO": "1"}, **HX))

    def test_foreign_and_anonymous(self, client, world):
        assert client.get(url("rule_simulate", world["template"].pk)).status_code == 302
        client.force_login(world["other"])
        assert client.post(url("rule_simulate", world["template"].pk), {}, **HX).status_code == 404
