"""The rules API: metadata, CRUD, validate, simulate and compiled - with the same services as the builder."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from apps.checklists import services
from apps.checklists.models import ChecklistRule
from apps.permissions import services as rbac
from apps.permissions.registry import P


def api(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


def data(response):
    payload = response.json()
    return payload.get("data", payload) if isinstance(payload, dict) else payload


def act(kind, target=None, target_type="item", **params):
    out = {"type": kind, **params}
    if target:
        out["target"] = {"type": target_type, "code": target}
    return out


@pytest.fixture
def world(org_a, org_b, make_user, assign):
    author = make_user(email="ra-author@example.com", organization=org_a, role=None)
    assign(author, org_a, "administrator")
    reader = make_user(email="ra-reader@example.com", organization=org_a, role=None)
    assign(reader, org_a, rbac.create_role(organization=org_a, name="Reader", permissions=[P.CHECKLIST_VIEW]))
    other = make_user(email="ra-other@example.com", organization=org_b, role=None)
    assign(other, org_b, "administrator")
    template = services.create_checklist_template(organization=org_a, name="Daily", code="DAY-1", actor=author)
    draft = template.versions.get()
    s1 = services.create_section(draft, name="Safety", actor=author)
    s2 = services.create_section(draft, name="Drive-through", actor=author)
    services.create_item(s1, question="Is the fire exit accessible?", code="FIRE", answer_type="yes_no", actor=author)
    services.create_item(s1, question="Refrigerator temperature", code="TEMP", answer_type="numeric", config={"min": -10, "max": 40, "decimals": 1, "unit": "°C"}, actor=author)
    services.create_item(s1, question="Describe the issue", code="NOTE", answer_type="long_text", is_required=False, actor=author)
    services.create_item(s2, question="Lane clear?", code="DT", answer_type="yes_no", actor=author)
    foreign = services.create_checklist_template(organization=org_b, name="Foreign", code="FX-1", actor=other)
    fs = services.create_section(foreign.versions.get(), name="F", actor=other)
    services.create_item(fs, question="Secret", code="SECRET", actor=other)
    foreign_rule = services.create_rule(foreign.versions.get(), name="Foreign rule", actions=[act("DISPLAY_MESSAGE", message="hidden")], actor=other)
    return {"author": author, "reader": reader, "other": other, "template": template, "draft": draft, "foreign": foreign, "foreign_rule": foreign_rule}


def url(world, tail=""):
    return f"/api/v1/checklists/{world['template'].pk}/rules/{tail}"


RULE = {
    "name": "Blocked exit",
    "condition": {"operator": "AND", "conditions": [{"source_item_code": "FIRE", "comparison": "EQUALS", "value": "no"}]},
    "actions": [act("SHOW_ITEM", "NOTE"), act("REQUIRE_COMMENT", "FIRE"), act("DISPLAY_MESSAGE", "FIRE", severity="warning", message="Report it.")],
}


@pytest.mark.django_db
class TestMetadata:
    def test_requires_authentication(self):
        assert APIClient().get("/api/v1/checklists/rules/metadata/").status_code in (401, 403)

    def test_operators_actions_and_limits(self, world):
        meta = data(api(world["reader"]).get("/api/v1/checklists/rules/metadata/"))
        codes = {o["code"] for o in meta["operators"]}
        assert {"EQUALS", "BETWEEN", "CONTAINS", "SELECTED", "CONTAINS_ALL", "IS_EMPTY"} <= codes
        assert "GREATER_THAN" not in meta["by_answer_type"]["boolean"] and "GREATER_THAN" in meta["by_answer_type"]["numeric"]
        assert {a["code"] for a in meta["actions"]} >= {"SHOW_ITEM", "HIDE_ITEM", "REQUIRE_ITEM", "MAKE_OPTIONAL", "SHOW_SECTION", "HIDE_SECTION",
                                                         "DISPLAY_MESSAGE", "REQUIRE_COMMENT", "REQUIRE_EVIDENCE", "SET_VALIDATION_CONSTRAINT", "OPTION_AVAILABILITY"}
        assert meta["limits"]["max_group_depth"] == 3

    def test_path_is_not_read_as_a_checklist_id(self, world):
        assert api(world["author"]).get("/api/v1/checklists/rules/metadata/").status_code == 200


@pytest.mark.django_db
class TestCrud:
    def test_create_list_get_patch_disable_delete(self, world):
        client = api(world["author"])
        created = client.post(url(world), RULE, format="json")
        assert created.status_code == 201, created.json()
        rule = data(created)
        assert rule["code"] == "R-001" and rule["condition"]["conditions"][0]["value"] == "no"
        assert rule["summary"].startswith("If “Is the fire exit accessible?” equals No")
        rows = data(client.get(url(world)))
        assert [r["code"] for r in rows] == ["R-001"]
        detail = url(world, f"{rule['id']}/")
        assert data(client.get(detail))["name"] == "Blocked exit"
        patched = client.patch(detail, {"name": "Renamed", "actions": [act("DISPLAY_MESSAGE", message="Only a message")]}, format="json")
        assert data(patched)["name"] == "Renamed" and data(patched)["actions"] == [{"type": "DISPLAY_MESSAGE", "severity": "info", "message": "Only a message"}]
        disabled = client.patch(detail, {"is_active": False}, format="json")
        assert data(disabled)["is_active"] is False
        assert client.delete(detail).status_code == 204
        assert not ChecklistRule.objects.filter(version=world["draft"]).exists()

    def test_code_cannot_be_changed(self, world):
        client = api(world["author"])
        rule = data(client.post(url(world), RULE, format="json"))
        client.patch(url(world, f"{rule['id']}/"), {"code": "HACK"}, format="json")
        assert ChecklistRule.objects.get(pk=rule["id"]).code == "R-001"

    @pytest.mark.parametrize("payload,field", [
        ({"name": "x", "actions": [act("SHOW_ITEM", "NOPE")]}, "actions"),
        ({"name": "x", "condition": {"source_item_code": "NOPE", "comparison": "EQUALS", "value": "x"}, "actions": [act("SHOW_ITEM", "NOTE")]}, "condition"),
        ({"name": "x", "condition": {"source_item_code": "FIRE", "comparison": "GREATER_THAN", "value": 3}, "actions": [act("SHOW_ITEM", "NOTE")]}, "condition"),
        ({"name": "x", "actions": []}, "actions"),
        ({"name": "x", "actions": "SHOW_ITEM"}, "actions"),
        ({"name": "x", "actions": [act("DELETE_EVERYTHING", "NOTE")]}, "actions"),
        ({"name": "x", "actions": [act("DISPLAY_MESSAGE", message="m", evil="1")]}, "actions"),
        ({"actions": [act("DISPLAY_MESSAGE", message="m")]}, "name"),
        ({"name": "", "actions": [act("DISPLAY_MESSAGE", message="m")]}, "name"),
    ])
    def test_invalid_payloads_are_400_with_field_errors_and_issues(self, world, payload, field):
        response = api(world["author"]).post(url(world), payload, format="json")
        assert response.status_code == 400, response.json()
        assert field in str(response.json())
        assert not ChecklistRule.objects.filter(version=world["draft"]).exists()

    def test_rule_validation_errors_carry_structured_issues(self, world):
        response = api(world["author"]).post(url(world), {"name": "x", "condition": {"source_item_code": "FIRE", "comparison": "GREATER_THAN", "value": 3},
                                                          "actions": [act("SHOW_ITEM", "NOTE")]}, format="json")
        body = response.json()
        body = body.get("errors", body.get("error", body))
        text = str(body)
        assert "invalid_operator" in text and "conditions[0].comparison" in text

    def test_cycle_is_rejected_over_the_api(self, world):
        client = api(world["author"])
        client.post(url(world), {"name": "a", "condition": {"source_item_code": "FIRE", "comparison": "EQUALS", "value": "no"}, "actions": [act("SHOW_ITEM", "DT")]}, format="json")
        response = client.post(url(world), {"name": "b", "condition": {"source_item_code": "DT", "comparison": "EQUALS", "value": "yes"}, "actions": [act("SHOW_ITEM", "FIRE")]}, format="json")
        assert response.status_code == 400 and "circle" in str(response.json())

    def test_oversized_json_is_rejected(self, world):
        huge = {"name": "x", "actions": [act("DISPLAY_MESSAGE", message="m")] * 2, "condition": {"operator": "AND", "conditions": [{"source_item_code": "FIRE", "comparison": "EQUALS", "value": "x" * 70000}]}}
        assert api(world["author"]).post(url(world), huge, format="json").status_code == 400

    def test_published_version_rules_are_read_only(self, world):
        client = api(world["author"])
        rule = data(client.post(url(world), RULE, format="json"))
        services.publish_checklist_version(world["draft"], actor=world["author"])
        assert client.post(url(world), RULE, format="json").status_code == 400  # no draft to edit
        detail = url(world, f"{rule['id']}/")
        assert client.patch(detail, {"name": "x"}, format="json").status_code == 400
        assert client.delete(detail).status_code == 400
        assert client.get(detail).status_code == 200 and data(client.get(url(world)))[0]["code"] == "R-001"
        assert data(client.get(url(world, "?version=1")))[0]["version"] == 1


@pytest.mark.django_db
class TestAuthorization:
    def test_unauthenticated(self, world):
        assert APIClient().get(url(world)).status_code in (401, 403)

    def test_reader_can_read_but_not_write(self, world):
        author_rule = data(api(world["author"]).post(url(world), RULE, format="json"))
        reader = api(world["reader"])
        assert reader.get(url(world)).status_code == 200
        assert reader.get(url(world, f"{author_rule['id']}/")).status_code == 200
        assert reader.post(url(world), RULE, format="json").status_code == 403
        assert reader.patch(url(world, f"{author_rule['id']}/"), {"name": "x"}, format="json").status_code == 403
        assert reader.delete(url(world, f"{author_rule['id']}/")).status_code == 403
        assert reader.post(url(world, "validate/"), {}, format="json").status_code == 403
        assert reader.post(url(world, "simulate/"), {"rules": [RULE]}, format="json").status_code == 403  # unsaved rules need edit rights
        assert reader.post(url(world, "simulate/"), {"answers": {"FIRE": "no"}}, format="json").status_code == 200  # saved rules: view is enough
        assert ChecklistRule.objects.get(pk=author_rule["id"]).name == "Blocked exit"

    def test_foreign_checklist_and_rule_ids_are_404(self, world):
        client = api(world["author"])
        foreign = world["foreign"].pk
        for tail in ("", "validate/", "simulate/", "compiled/"):
            method = client.get if tail in ("", "compiled/") else client.post
            assert method(f"/api/v1/checklists/{foreign}/rules/{tail}").status_code == 404, tail
        # A foreign rule id under *my* checklist must not resolve either (IDOR).
        mine = url(world, f"{world['foreign_rule'].pk}/")
        assert client.get(mine).status_code == 404
        assert client.patch(mine, {"name": "hacked"}, format="json").status_code == 404
        assert client.delete(mine).status_code == 404
        world["foreign_rule"].refresh_from_db()
        assert world["foreign_rule"].name == "Foreign rule"

    def test_other_tenant_user_cannot_use_my_checklist(self, world):
        client = api(world["other"])
        assert client.get(url(world)).status_code == 404
        assert client.post(url(world), RULE, format="json").status_code == 404

    def test_cross_tenant_question_codes_do_not_resolve(self, world):
        response = api(world["author"]).post(url(world), {"name": "x", "condition": {"source_item_code": "SECRET", "comparison": "EQUALS", "value": "yes"},
                                                          "actions": [act("SHOW_ITEM", "NOTE")]}, format="json")
        assert response.status_code == 400 and "Unknown question" in str(response.json())


@pytest.mark.django_db
class TestValidateAndSimulate:
    def test_validate_candidates_without_saving(self, world):
        client = api(world["author"])
        ok = data(client.post(url(world, "validate/"), {"rules": [RULE]}, format="json"))
        assert ok["valid"] is True and ok["errors"] == []
        bad = data(client.post(url(world, "validate/"), {"rules": [{"name": "x", "condition": {"source_item_code": "NOPE", "comparison": "EQUALS", "value": 1}, "actions": []}]}, format="json"))
        assert bad["valid"] is False and {e["code"] for e in bad["errors"]} >= {"unknown_item", "invalid_rule"}
        assert not ChecklistRule.objects.filter(version=world["draft"]).exists()

    def test_validate_reports_cycles_and_conflicts(self, world):
        a = {"code": "A", "name": "a", "condition": {"source_item_code": "FIRE", "comparison": "EQUALS", "value": "no"}, "actions": [act("SHOW_ITEM", "DT")]}
        b = {"code": "B", "name": "b", "condition": {"source_item_code": "DT", "comparison": "EQUALS", "value": "yes"}, "actions": [act("SHOW_ITEM", "FIRE")]}
        result = data(api(world["author"]).post(url(world, "validate/"), {"rules": [a, b]}, format="json"))
        assert [e["code"] for e in result["errors"]] == ["cycle"]

    def test_simulate_scenarios(self, world):
        client = api(world["author"])
        client.post(url(world), RULE, format="json")
        result = data(client.post(url(world, "simulate/"), {"answers": {"FIRE": "no"}}, format="json"))
        assert result["simulation"] is True
        assert result["items"]["NOTE"]["visible"] is True and result["items"]["FIRE"]["comment_required"] is True
        assert result["messages"] == [{"severity": "warning", "message": "Report it.", "rule": "R-001", "target": {"type": "item", "code": "FIRE"}}]
        yes = data(client.post(url(world, "simulate/"), {"answers": {"FIRE": "yes"}}, format="json"))
        assert yes["items"]["NOTE"]["visible"] is False and yes["messages"] == []
        none = data(client.post(url(world, "simulate/"), {"answers": {}}, format="json"))
        assert none["rules"][0]["matched"] == "unknown" and none["items"]["NOTE"]["visible"] is False

    def test_simulate_unsaved_rules_for_authors(self, world):
        result = data(api(world["author"]).post(url(world, "simulate/"), {"answers": {"TEMP": "9"}, "rules": [{
            "name": "hot", "condition": {"source_item_code": "TEMP", "comparison": "GREATER_THAN", "value": 5},
            "actions": [act("DISPLAY_MESSAGE", "TEMP", severity="error", message="Too warm")]}]}, format="json"))
        assert result["messages"][0]["message"] == "Too warm"
        assert not ChecklistRule.objects.filter(version=world["draft"]).exists()

    def test_simulation_persists_nothing(self, world):
        from apps.core.models import ActivityLog

        client = api(world["author"])
        client.post(url(world), RULE, format="json")
        before = (ActivityLog.objects.count(), ChecklistRule.objects.count())
        for _ in range(3):
            client.post(url(world, "simulate/"), {"answers": {"FIRE": "no", "TEMP": "7"}}, format="json")
        assert (ActivityLog.objects.count(), ChecklistRule.objects.count()) == before

    def test_simulation_payload_is_bounded(self, world, settings):
        client = api(world["author"])
        settings.CHECKLIST_RULE_LIMITS = {"max_simulation_answers": 3, "max_simulation_value_chars": 50}
        assert client.post(url(world, "simulate/"), {"answers": {f"Q{i}": "x" for i in range(4)}}, format="json").status_code == 400
        assert client.post(url(world, "simulate/"), {"answers": {"NOTE": "x" * 100}}, format="json").status_code == 400
        assert client.post(url(world, "simulate/"), {"answers": {"x" * 60: "y"}}, format="json").status_code == 400
        assert client.post(url(world, "simulate/"), {"answers": "not a dict"}, format="json").status_code == 400
        assert client.post(url(world, "simulate/"), {"version": "x"}, format="json").status_code == 400
        settings.CHECKLIST_RULE_LIMITS = {"max_simulation_payload_bytes": 100}
        response = client.post(url(world, "simulate/"), {"answers": {"NOTE": "y" * 500}}, format="json")
        assert response.status_code == 413

    def test_malicious_answers_are_just_values(self, world):
        client = api(world["author"])
        payload = {"FIRE": "__import__('os').system('x')", "NOTE": "<script>alert(1)</script>", "TEMP": {"$gt": ""}, "NOPE": "x"}
        response = client.post(url(world, "simulate/"), {"answers": payload}, format="json")
        assert response.status_code == 200
        body = data(response)
        assert body["items"]["FIRE"]["answer"]["status"] == "invalid" and body["items"]["TEMP"]["answer"]["status"] == "invalid"
        assert any(e["code"] == "unknown_item" for e in body["errors"])

    def test_simulate_a_specific_version(self, world):
        client = api(world["author"])
        client.post(url(world), RULE, format="json")
        services.publish_checklist_version(world["draft"], actor=world["author"])
        v2 = services.create_checklist_version(world["template"], actor=world["author"])
        services.delete_rule(v2.rules.get(), actor=world["author"])
        v1 = data(client.post(url(world, "simulate/"), {"version": 1, "answers": {"FIRE": "no"}}, format="json"))
        v2r = data(client.post(url(world, "simulate/"), {"version": 2, "answers": {"FIRE": "no"}}, format="json"))
        assert v1["messages"] and not v2r["messages"] and (v1["version"], v2r["version"]) == (1, 2)
        assert client.post(url(world, "simulate/"), {"version": 9}, format="json").status_code == 404

    def test_compiled_contract_for_frontends(self, world):
        client = api(world["author"])
        client.post(url(world), RULE, format="json")
        compiled = data(client.get(url(world, "compiled/")))
        assert compiled["valid"] is True and compiled["rules"][0]["code"] == "R-001"
        assert compiled["rules"][0]["condition"]["conditions"][0] == {"source_item_code": "FIRE", "comparison": "EQUALS", "value": "no"}
        assert compiled["items"]["TEMP"]["config"]["unit"] == "°C" and compiled["items"]["FIRE"]["answer_type"] == "yes_no"
        assert compiled["sections"]["S-002"]["name"] == "Drive-through"
        assert api(world["reader"]).get(url(world, "compiled/")).status_code == 200  # view permission is enough
