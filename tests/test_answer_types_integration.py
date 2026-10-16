"""Answer types wired into the product: items, services, builder UI, API, imports and the workbook seed data."""

from __future__ import annotations

import json

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.urls import reverse
from rest_framework.test import APIClient

from apps.checklists import imports, selectors, services
from apps.checklists.answer_types import INVALID, validate_answer
from apps.checklists.metadata import clean_metadata
from apps.checklists.models import ChecklistItem, ChecklistTemplate
from apps.core.models import ActivityLog
from apps.permissions import services as rbac
from apps.permissions.registry import P


def body(response) -> str:
    return response.content.decode()


def data(response):
    payload = response.json()
    return payload.get("data", payload) if isinstance(payload, dict) else payload


@pytest.fixture
def world(org_a, org_b, make_user, assign):
    author = make_user(email="at-author@example.com", organization=org_a, role=None)
    assign(author, org_a, "administrator")
    reader = make_user(email="at-reader@example.com", organization=org_a, role=None)
    assign(reader, org_a, rbac.create_role(organization=org_a, name="Reader", permissions=[P.CHECKLIST_VIEW]))
    other = make_user(email="at-other@example.com", organization=org_b, role=None)
    assign(other, org_b, "administrator")
    template = services.create_checklist_template(organization=org_a, name="Daily", code="DAY-1", actor=author)
    section = services.create_section(template.versions.get(), name="Checks", actor=author)
    foreign = services.create_checklist_template(organization=org_b, name="Foreign", code="FX-1", actor=other)
    foreign_section = services.create_section(foreign.versions.get(), name="F", actor=other)
    return {"author": author, "reader": reader, "other": other, "template": template, "section": section,
            "foreign": foreign, "foreign_section": foreign_section}


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestValidateAgainstItems:
    def test_choice_options_are_the_items_own(self, world):
        a = services.create_item(world["section"], question="A", answer_type="single_choice", options=[("x", "X"), ("y", "Y")], actor=world["author"])
        b = services.create_item(world["section"], question="B", answer_type="single_choice", options=[("p", "P"), ("q", "Q")], actor=world["author"])
        assert validate_answer(a, "x").value == "x"
        assert validate_answer(a, str(a.options.get(value="y").pk)).value == "y"
        # B's option - by value or by id - never resolves against A.
        assert validate_answer(a, "p").status == INVALID
        assert validate_answer(a, str(b.options.get(value="p").pk)).errors[0].code == "invalid_option"

    def test_foreign_organization_option_injection_is_rejected(self, world):
        mine = services.create_item(world["section"], question="Mine", answer_type="single_choice", options=[("x", "X"), ("y", "Y")], actor=world["author"])
        theirs = services.create_item(world["foreign_section"], question="Theirs", answer_type="multiple_choice",
                                      options=[("secret", "Secret"), ("other", "Other")], actor=world["other"])
        foreign_option = theirs.options.get(value="secret")
        assert validate_answer(mine, str(foreign_option.pk)).status == INVALID
        assert validate_answer(mine, "secret").status == INVALID
        multi = services.create_item(world["section"], question="Multi", answer_type="multiple_choice", options=[("a", "A"), ("b", "B")], actor=world["author"])
        assert validate_answer(multi, ["a", str(foreign_option.pk)]).status == INVALID

    def test_scope_assertions_for_forged_item_ids(self, world, org_a, org_b):
        item = services.create_item(world["section"], question="Q", actor=world["author"])
        assert validate_answer(item, "yes", organization=org_a).valid
        assert validate_answer(item, "yes", organization=org_b).errors[0].code == "scope"
        assert validate_answer(item, "yes", version=world["template"].versions.get()).valid
        assert validate_answer(item, "yes", version=world["foreign"].versions.get()).errors[0].code == "scope"

    def test_inactive_option_and_item_are_not_answerable(self, world):
        item = services.create_item(world["section"], question="Q", answer_type="single_choice", options=[("a", "A"), ("b", "B")], actor=world["author"])
        services.update_option(item.options.get(value="a"), is_active=False, actor=world["author"])
        item = ChecklistItem.objects.prefetch_related("options").get(pk=item.pk)
        assert validate_answer(item, "a").status == INVALID and validate_answer(item, "b").valid
        services.update_item(item, is_active=False, actor=world["author"])
        item.refresh_from_db()
        assert validate_answer(item, "b").errors[0].code == "item_inactive"

    def test_item_flags_drive_required_and_the_override(self, world):
        required = services.create_item(world["section"], question="R", answer_type="numeric", actor=world["author"])
        optional = services.create_item(world["section"], question="O", answer_type="numeric", is_required=False, actor=world["author"])
        assert validate_answer(required, None).errors[0].code == "required"
        assert validate_answer(optional, None).status == "empty"
        assert validate_answer(optional, None, required=True).status == INVALID  # a rule can make it required
        assert validate_answer(required, None, required=False).status == "empty"
        assert validate_answer(required, None, not_applicable=True).status == "not_applicable"

    def test_legacy_phase4_configs_are_read_with_defaults(self, world):
        item = services.create_item(world["section"], question="Rate", answer_type="score", actor=world["author"])
        ChecklistItem.objects.filter(pk=item.pk).update(config={"min": 1, "max": 5})  # as stored by Phase 4
        item.refresh_from_db()
        assert validate_answer(item, 3).value == 3 and validate_answer(item, 6).status == INVALID

    def test_validation_uses_prefetched_options(self, world, django_assert_num_queries):
        item = services.create_item(world["section"], question="Q", answer_type="multiple_choice", options=[("a", "A"), ("b", "B")], actor=world["author"])
        item = ChecklistItem.objects.prefetch_related("options").get(pk=item.pk)
        with django_assert_num_queries(0):
            assert validate_answer(item, ["a", "b"]).value == ["a", "b"]


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestServicesAndVersionSafety:
    def test_config_is_normalized_and_validated_on_save(self, world):
        item = services.create_item(world["section"], question="Temp", answer_type="numeric",
                                    config={"min": "-10", "max": 20, "unit": "°C", "decimals": 1}, actor=world["author"])
        assert item.config == {"min": -10, "max": 20, "decimals": 1, "unit": "°C"}
        with pytest.raises(ValidationError) as info:
            services.update_item(item, config={"min": 30, "max": 20}, actor=world["author"])
        assert "config" in info.value.message_dict

    def test_answer_type_and_config_changes_are_logged_separately(self, world):
        item = services.create_item(world["section"], question="Rate", answer_type="score", actor=world["author"])
        services.update_item(item, config={"min": 0, "max": 10}, actor=world["author"])
        services.update_item(item, answer_type="numeric", actor=world["author"])
        verbs = set(ActivityLog.objects.values_list("verb", flat=True))
        assert {"checklist_item.config_changed", "checklist_item.answer_type_changed"} <= verbs
        # The type change also reset the config, so there are two config events; each carries the template id.
        changes = ActivityLog.objects.filter(verb="checklist_item.config_changed")
        assert changes.count() == 2 and all(c.metadata["template_id"] == str(world["template"].pk) for c in changes)
        assert item.history.count() >= 3

    def test_published_versions_keep_their_configuration(self, world):
        item = services.create_item(world["section"], question="Temperature", answer_type="numeric", config={"min": 0, "max": 10}, actor=world["author"])
        v1 = world["template"].versions.get()
        services.publish_checklist_version(v1, actor=world["author"])
        with pytest.raises(ValidationError):
            services.update_item(item, config={"min": 0, "max": 20}, actor=world["author"])
        v2 = services.create_checklist_version(world["template"], actor=world["author"])
        new_item = ChecklistItem.objects.get(section__version=v2)
        services.update_item(new_item, config={"min": 0, "max": 20}, actor=world["author"])
        item.refresh_from_db(), new_item.refresh_from_db()
        assert item.config["max"] == 10 and new_item.config["max"] == 20
        assert validate_answer(item, "15").status == INVALID and validate_answer(new_item, "15").valid

    def test_set_options_syncs_in_one_audited_step(self, world):
        item = services.create_item(world["section"], question="Pick", answer_type="single_choice", options=[("a", "A"), ("b", "B"), ("c", "C")], actor=world["author"])
        keep = item.options.get(value="b")
        services.set_options(item, [{"value": "c", "label": "Charlie"}, "Delta", ("b", "Bravo")], actor=world["author"])
        assert list(item.options.order_by("sort_order").values_list("value", "label")) == [("c", "Charlie"), ("delta", "Delta"), ("b", "Bravo")]
        assert item.options.get(value="b").pk == keep.pk and not item.options.filter(value="a").exists()
        event = ActivityLog.objects.get(verb="checklist_option.synced")
        assert event.metadata["added"] == ["delta"] and event.metadata["removed"] == ["a"]

    @pytest.mark.parametrize("options", [["A", "a"], [""], [{"value": "BAD VALUE", "label": "x"}], [{"label": "x", "extra": 1}], [5], ["x"] * 101])
    def test_bad_option_input_is_rejected(self, world, options):
        item = services.create_item(world["section"], question="Pick", answer_type="single_choice", actor=world["author"])
        with pytest.raises(ValidationError):
            services.set_options(item, options, actor=world["author"])

    def test_options_cannot_be_set_on_other_types(self, world):
        item = services.create_item(world["section"], question="N", answer_type="numeric", actor=world["author"])
        with pytest.raises(ValidationError):
            services.set_options(item, ["a"], actor=world["author"])

    def test_metadata_is_validated(self, world):
        item = services.create_item(world["section"], question="Q", metadata={"points": 100, "critical": True, "applies_to": ["ksa", "EGP"]}, actor=world["author"])
        assert item.metadata == {"points": 100, "critical": True, "applies_to": ["EGP", "KSA"]}
        for bad in ({"points": -1}, {"points": "x"}, {"critical": "yes"}, {"colour": "red"}, {"applies_to": ["not valid!"]}, {"reference": "x" * 200}, "str"):
            with pytest.raises(ValidationError):
                clean_metadata(bad)
        services.update_item(item, question="Reworded", actor=world["author"])
        item.refresh_from_db()
        assert item.metadata["points"] == 100  # an edit that does not mention metadata keeps it


# ---------------------------------------------------------------------------
def url(name, *args):
    return reverse(f"checklists:{name}", args=args)


@pytest.mark.django_db
class TestBuilderUI:
    def test_fields_partial_per_type(self, client, world):
        client.force_login(world["author"])
        t = world["template"]
        html = body(client.get(url("answer_type_fields", t.pk), {"answer_type": "score"}))
        assert 'name="cfg-min"' in html and 'name="cfg-max"' in html and 'name="cfg-step"' in html and 'name="cfg-display"' in html
        html = body(client.get(url("answer_type_fields", t.pk), {"answer_type": "numeric"}))
        assert 'name="cfg-decimals"' in html and 'name="cfg-unit"' in html and 'name="cfg-step"' not in html
        html = body(client.get(url("answer_type_fields", t.pk), {"answer_type": "matrix"}))
        assert 'name="cfg-rows"' in html and 'name="cfg-columns"' in html and 'name="cfg-mode"' in html
        html = body(client.get(url("answer_type_fields", t.pk), {"answer_type": "yes_no"}))
        assert "no settings" in html and "cfg-" not in html
        html = body(client.get(url("answer_type_fields", t.pk), {"answer_type": "single_choice"}))
        assert "Add the choices after saving" in html
        html = body(client.get(url("answer_type_fields", t.pk), {"answer_type": "nonsense"}))
        assert "Yes / No" in html  # unknown types fall back, never error

    def test_fields_partial_prefills_from_the_item_only_for_its_own_type(self, client, world):
        client.force_login(world["author"])
        t = world["template"]
        item = services.create_item(world["section"], question="Temp", answer_type="numeric", config={"min": 3, "max": 9, "unit": "kg"}, actor=world["author"])
        html = body(client.get(url("answer_type_fields", t.pk), {"answer_type": "numeric", "item": item.pk}))
        assert 'value="kg"' in html
        html = body(client.get(url("answer_type_fields", t.pk), {"answer_type": "score", "item": item.pk}))
        assert 'value="kg"' not in html

    def test_item_editor_has_grouped_selector_description_and_preview_hooks(self, client, world):
        client.force_login(world["author"])
        html = body(client.get(url("item_new", world["template"].pk, world["section"].pk)))
        for group in ("Choice", "Value", "Date &amp; Time", "Media", "Advanced"):
            assert f'<optgroup label="{group}">' in html
        assert 'hx-get="' + url("answer_type_fields", world["template"].pk) in html
        assert 'id="answer-preview"' in html and 'aria-describedby="answer-type-help"' in html
        assert 'id="answer-type-help"' in html

    @pytest.mark.parametrize("answer_type,params,expect", [
        ("boolean", {}, ["True", "False"]),
        ("yes_no", {}, ["Yes", "No"]),
        ("yes_no_na", {}, ["N/A"]),
        ("score", {"cfg-min": "0", "cfg-max": "10", "cfg-display": "numeric"}, ["0 to 10"]),
        ("score", {"cfg-min": "1", "cfg-max": "5", "cfg-display": "stars"}, ['data-lucide="star"']),
        ("numeric", {"cfg-min": "0", "cfg-max": "10", "cfg-decimals": "1", "cfg-unit": "°C"}, ["°C", "Range 0 to 10", "1 decimal place"]),
        ("text", {"cfg-max_length": "50"}, ["Up to 50 characters"]),
        ("long_text", {}, ["textarea"]),
        ("date", {}, ['type="date"']),
        ("datetime", {}, ['type="datetime-local"']),
        ("photo", {"cfg-max_files": "3"}, ["Up to 3 files", "JPG"]),
        ("file", {}, ["Attach a file", "PDF"]),
        ("signature", {"cfg-signer_label": "Auditor", "cfg-require_name": "on"}, ["Auditor signs here", "Printed name"]),
        ("matrix", {"cfg-rows": "Dining Area\nKitchen", "cfg-columns": "Clean\nDamaged", "cfg-mode": "single"}, ["Dining Area", "Damaged", 'type="radio"']),
        ("matrix", {"cfg-rows": "A", "cfg-columns": "B", "cfg-mode": "multiple"}, ['type="checkbox"']),
        ("single_choice", {"cfg-display": "dropdown"}, ["<select", "Option 1", "Sample options"]),
        ("multiple_choice", {"cfg-min_select": "1", "cfg-max_select": "2"}, ["Option 2", "Select at least 1", "Select at most 2"]),
    ])
    def test_preview_renders_every_type(self, client, world, answer_type, params, expect):
        client.force_login(world["author"])
        html = body(client.get(url("answer_type_preview", world["template"].pk), {"answer_type": answer_type, "question": "Is it ok?", **params}))
        assert "Is it ok?" in html and "inert" in html
        for text in expect:
            assert text in html, text

    def test_preview_shows_the_items_real_options_and_reports_bad_settings(self, client, world):
        client.force_login(world["author"])
        item = services.create_item(world["section"], question="Pick", answer_type="single_choice", options=[("e", "Excellent"), ("g", "Good")], actor=world["author"])
        html = body(client.get(url("answer_type_preview", world["template"].pk), {"answer_type": "single_choice", "item": item.pk}))
        assert "Excellent" in html and "Sample options" not in html
        html = body(client.get(url("answer_type_preview", world["template"].pk), {"answer_type": "score", "cfg-min": "9", "cfg-max": "3"}))
        assert "minimum score must be below" in html.lower() or "must be below the maximum" in html

    def test_preview_escapes_the_question(self, client, world):
        client.force_login(world["author"])
        html = body(client.get(url("answer_type_preview", world["template"].pk), {"answer_type": "text", "question": "<img src=x onerror=alert(1)>"}))
        assert "<img src=x" not in html and "&lt;img" in html

    def test_partials_are_protected(self, client, world):
        t = world["template"]
        assert client.get(url("answer_type_fields", t.pk)).status_code == 302  # anonymous -> sign in
        client.force_login(world["reader"])
        assert client.get(url("answer_type_fields", t.pk)).status_code == 403
        assert client.get(url("answer_type_preview", t.pk)).status_code == 403
        client.force_login(world["author"])
        foreign = world["foreign"]
        assert client.get(url("answer_type_fields", foreign.pk)).status_code == 404
        assert client.get(url("answer_type_preview", foreign.pk)).status_code == 404

    def test_partials_ignore_foreign_item_ids(self, client, world):
        client.force_login(world["author"])
        theirs = services.create_item(world["foreign_section"], question="Secret question", answer_type="numeric", config={"unit": "SECRETUNIT"}, actor=world["other"])
        html = body(client.get(url("answer_type_fields", world["template"].pk), {"answer_type": "numeric", "item": theirs.pk}))
        assert "SECRETUNIT" not in html

    def test_published_template_has_no_editor_partials(self, client, world):
        services.create_item(world["section"], question="Q", actor=world["author"])
        services.publish_checklist_version(world["template"].versions.get(), actor=world["author"])
        client.force_login(world["author"])
        assert client.get(url("answer_type_fields", world["template"].pk)).status_code == 404

    @pytest.mark.parametrize("answer_type,post,expected", [
        ("score", {"cfg-min": "0", "cfg-max": "10", "cfg-step": "2", "cfg-display": "slider"}, {"min": 0, "max": 10, "step": 2, "display": "slider"}),
        ("numeric", {"cfg-min": "-10", "cfg-max": "20", "cfg-decimals": "1", "cfg-unit": "°C"}, {"min": -10, "max": 20, "decimals": 1, "unit": "°C"}),
        ("text", {"cfg-min_length": "5", "cfg-max_length": "250"}, {"min_length": 5, "max_length": 250}),
        ("multiple_choice", {"cfg-min_select": "1"}, {"min_select": 1}),
        ("date", {"cfg-min_date": "2026-01-01", "cfg-max_date": "2026-12-31"}, {"min_date": "2026-01-01", "max_date": "2026-12-31"}),
        ("photo", {"cfg-max_files": "2", "cfg-max_size_mb": "5", "cfg-allowed_extensions": ["jpg", "png"], "cfg-source": "camera"},
         {"max_files": 2, "max_size_mb": 5, "allowed_extensions": ["jpg", "png"], "source": "camera"}),
        ("signature", {"cfg-signer_label": "Auditor"}, {"signer_label": "Auditor", "require_name": False}),
        ("matrix", {"cfg-rows": "Dining Area\nKitchen\n", "cfg-columns": "Clean\nDamaged", "cfg-mode": "multiple", "cfg-require_all_rows": "on"},
         {"rows": [{"key": "dining_area", "label": "Dining Area"}, {"key": "kitchen", "label": "Kitchen"}],
          "columns": [{"key": "clean", "label": "Clean"}, {"key": "damaged", "label": "Damaged"}], "mode": "multiple", "require_all_rows": True}),
    ])
    def test_each_types_settings_are_saved_through_the_form(self, client, world, answer_type, post, expected):
        client.force_login(world["author"])
        response = client.post(url("item_new", world["template"].pk, world["section"].pk),
                               {"question": f"Q {answer_type}", "answer_type": answer_type, "is_active": "on", **post})
        assert response.status_code == 302, body(response)[:2000]
        item = ChecklistItem.objects.get(section=world["section"], question=f"Q {answer_type}")
        for key, value in expected.items():
            assert item.config[key] == value, key

    def test_settings_errors_are_shown_and_nothing_is_saved(self, client, world):
        client.force_login(world["author"])
        for answer_type, post in (("score", {"cfg-min": "5", "cfg-max": "1"}), ("numeric", {"cfg-min": "x"}),
                                  ("matrix", {"cfg-rows": "", "cfg-columns": "A"}), ("text", {"cfg-max_length": "99999"})):
            response = client.post(url("item_new", world["template"].pk, world["section"].pk),
                                   {"question": "Bad", "answer_type": answer_type, "is_active": "on", **post})
            assert response.status_code == 200, answer_type
        assert not ChecklistItem.objects.filter(section=world["section"]).exists()

    def test_forged_type_and_extra_settings_are_rejected(self, client, world):
        client.force_login(world["author"])
        response = client.post(url("item_new", world["template"].pk, world["section"].pk),
                               {"question": "Bad", "answer_type": "telepathy", "is_active": "on"})
        assert response.status_code == 200 and not ChecklistItem.objects.filter(section=world["section"]).exists()
        # Settings of another type are simply not read (only the selected type's fields exist).
        response = client.post(url("item_new", world["template"].pk, world["section"].pk),
                               {"question": "OK", "answer_type": "boolean", "is_active": "on", "cfg-min": "3", "cfg-evil": "x"})
        assert response.status_code == 302 and ChecklistItem.objects.get(section=world["section"]).config == {}

    def test_editing_an_item_keeps_and_changes_settings(self, client, world):
        client.force_login(world["author"])
        item = services.create_item(world["section"], question="T", answer_type="numeric", config={"min": 0, "max": 10, "unit": "kg"}, actor=world["author"])
        html = body(client.get(url("item_edit", world["template"].pk, item.pk)))
        assert 'value="kg"' in html and 'name="cfg-max"' in html
        client.post(url("item_edit", world["template"].pk, item.pk),
                    {"question": "T", "code": item.code, "answer_type": "numeric", "is_active": "on", "cfg-min": "0", "cfg-max": "20", "cfg-unit": "kg", "cfg-decimals": "1"})
        item.refresh_from_db()
        assert item.config == {"min": 0, "max": 20, "decimals": 1, "unit": "kg"}

    def test_structure_shows_points_and_critical(self, client, world):
        services.create_item(world["section"], question="Probes work?", answer_type="yes_no_na", metadata={"points": 100, "critical": True}, actor=world["author"])
        client.force_login(world["author"])
        html = body(client.get(url("builder", world["template"].pk)))
        assert "Critical" in html
        services.publish_checklist_version(world["template"].versions.get(), actor=world["author"])
        html = body(client.get(url("detail", world["template"].pk)))
        assert "100 pts" in html and "Critical" in html


# ---------------------------------------------------------------------------
def api(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.mark.django_db
class TestAnswerTypeAPI:
    def test_requires_authentication(self):
        assert APIClient().get("/api/v1/checklists/answer-types/").status_code in (401, 403)

    def test_list_metadata(self, world):
        rows = data(api(world["reader"]).get("/api/v1/checklists/answer-types/"))
        by = {r["code"]: r for r in rows}
        assert len(rows) == 15 and set(by) >= {"boolean", "score", "matrix", "signature"}
        assert by["score"]["category"] == "value" and by["score"]["label"] == "Score"
        assert by["single_choice"]["capabilities"]["requires_options"] is True
        assert by["boolean"]["capabilities"]["supports_rules"] is True
        assert "config_schema" not in by["score"]  # the list stays small

    def test_detail_schema_and_defaults(self, world):
        info = data(api(world["reader"]).get("/api/v1/checklists/answer-types/score/"))
        assert info["default_config"] == {"min": 1, "max": 5, "step": 1, "display": "numeric"}
        assert {f["key"] for f in info["config_schema"]} == {"min", "max", "step", "default", "display"}
        assert info["example_value"] == 4
        assert data(api(world["reader"]).get("/api/v1/checklists/answer-types/NUMERIC/"))["code"] == "numeric"
        assert api(world["reader"]).get("/api/v1/checklists/answer-types/telepathy/").status_code == 404

    def test_answer_types_path_is_not_read_as_a_checklist_id(self, world):
        assert api(world["author"]).get("/api/v1/checklists/answer-types/").status_code == 200

    def test_item_create_update_delete_use_the_same_validation(self, world):
        client = api(world["author"])
        pk = world["template"].pk
        created = client.post(f"/api/v1/checklists/{pk}/items/", {
            "section": str(world["section"].pk), "question": "Fridge temperature", "answer_type": "numeric",
            "config": {"min": -10, "max": 20, "decimals": 1, "unit": "°C"}, "metadata": {"points": 100}}, format="json")
        assert created.status_code == 201, created.json()
        item = data(created)
        assert item["config"] == {"min": -10, "max": 20, "decimals": 1, "unit": "°C"} and item["metadata"] == {"points": 100}
        item_url = f"/api/v1/checklists/{pk}/items/{item['id']}/"
        patched = client.patch(item_url, {"question": "Fridge temp", "config": {"min": -10, "max": 25, "decimals": 1, "unit": "°C"}}, format="json")
        assert patched.status_code == 200 and data(patched)["config"]["max"] == 25
        assert client.get(item_url).status_code == 200
        assert client.delete(item_url).status_code == 204
        assert not ChecklistItem.objects.filter(pk=item["id"]).exists()

    def test_item_with_options_and_option_replacement(self, world):
        client = api(world["author"])
        pk = world["template"].pk
        created = client.post(f"/api/v1/checklists/{pk}/items/", {
            "section": str(world["section"].pk), "question": "Issues?", "answer_type": "multiple_choice",
            "options": ["Cleaning", {"value": "safety", "label": "Safety"}, "Training"]}, format="json")
        assert created.status_code == 201
        item = data(created)
        assert [o["value"] for o in item["options"]] == ["cleaning", "safety", "training"]
        patched = client.patch(f"/api/v1/checklists/{pk}/items/{item['id']}/", {"options": ["Safety", "Documentation"]}, format="json")
        assert [o["value"] for o in data(patched)["options"]] == ["safety", "documentation"]

    @pytest.mark.parametrize("payload,field", [
        ({"answer_type": "score", "config": {"min": 5, "max": 1}}, "config"),
        ({"answer_type": "telepathy"}, "answer_type"),
        ({"answer_type": "boolean", "config": {"min": 1}}, "config"),
        ({"answer_type": "numeric", "config": {"unit": "x" * 30}}, "config"),
        ({"answer_type": "yes_no", "options": ["a", "b"]}, None),
        ({"metadata": {"colour": "red"}}, "metadata"),
        ({"answer_type": "single_choice", "options": [{"label": "A", "evil": 1}]}, "options"),
    ])
    def test_invalid_item_payloads_are_400_with_field_errors(self, world, payload, field):
        client = api(world["author"])
        body_ = {"section": str(world["section"].pk), "question": "Bad", **payload}
        response = client.post(f"/api/v1/checklists/{world['template'].pk}/items/", body_, format="json")
        if field is None:  # options on a fixed-option type are silently not applicable
            assert response.status_code == 201
        else:
            assert response.status_code == 400, response.json()
            assert not ChecklistItem.objects.filter(question="Bad").exists()

    def test_missing_fields_and_unknown_section(self, world):
        client = api(world["author"])
        pk = world["template"].pk
        assert client.post(f"/api/v1/checklists/{pk}/items/", {"question": "x"}, format="json").status_code == 400
        assert client.post(f"/api/v1/checklists/{pk}/items/", {"section": str(world["foreign_section"].pk), "question": "x"}, format="json").status_code == 400

    def test_authorization_and_isolation(self, world):
        pk, foreign = world["template"].pk, world["foreign"].pk
        payload = {"section": str(world["section"].pk), "question": "Nope"}
        assert api(world["reader"]).post(f"/api/v1/checklists/{pk}/items/", payload, format="json").status_code == 403
        assert api(world["author"]).post(f"/api/v1/checklists/{foreign}/items/", payload, format="json").status_code == 404
        theirs = services.create_item(world["foreign_section"], question="Theirs", actor=world["other"])
        assert api(world["author"]).get(f"/api/v1/checklists/{pk}/items/{theirs.pk}/").status_code == 404
        assert api(world["author"]).patch(f"/api/v1/checklists/{pk}/items/{theirs.pk}/", {"question": "H"}, format="json").status_code == 404
        assert api(world["author"]).delete(f"/api/v1/checklists/{pk}/items/{theirs.pk}/").status_code == 404
        theirs.refresh_from_db()
        assert theirs.question == "Theirs"

    def test_published_version_cannot_be_changed_through_the_api(self, world):
        item = services.create_item(world["section"], question="Q", actor=world["author"])
        services.publish_checklist_version(world["template"].versions.get(), actor=world["author"])
        client = api(world["author"])
        pk = world["template"].pk
        assert client.post(f"/api/v1/checklists/{pk}/items/", {"section": str(world["section"].pk), "question": "N"}, format="json").status_code == 400
        assert client.patch(f"/api/v1/checklists/{pk}/items/{item.pk}/", {"question": "H"}, format="json").status_code == 400
        item.refresh_from_db()
        assert item.question == "Q"
        assert client.get(f"/api/v1/checklists/{pk}/items/{item.pk}/").status_code == 200  # reading is fine


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestImportWithAnswerTypes:
    def test_row_errors_name_the_row_and_roll_back(self, world, org_a):
        rows = [
            {"template": "DAY-1", "section": "Checks", "question": "Fine", "answer_type": "score", "config": '{"min": 0, "max": 10}', "points": "100", "critical": "yes", "applies_to": "ksa, egp"},
            {"template": "DAY-1", "section": "Checks", "question": "Bad range", "answer_type": "score", "config": '{"min": 9, "max": 1}'},
            {"template": "DAY-1", "section": "Checks", "question": "Bad points", "answer_type": "yes_no", "points": "lots"},
            {"template": "DAY-1", "section": "Checks", "question": "Bad type", "answer_type": "telepathy"},
        ]
        with pytest.raises(ValidationError) as info:
            imports.import_items(user=world["author"], organization=org_a, rows=rows)
        text = " ".join(info.value.messages)
        assert "Row 3" in text and "Row 4" in text and "Row 5" in text and "Row 2" not in text
        assert not ChecklistItem.objects.filter(section=world["section"]).exists()

    def test_metadata_and_options_import(self, world, org_a):
        imports.import_items(user=world["author"], organization=org_a, rows=[
            {"template": "DAY-1", "section": "Checks", "question": "Probes ok?", "answer_type": "yes_no_na", "points": "100", "critical": "true", "applies_to": "ksa,egp"},
            {"template": "DAY-1", "section": "Checks", "question": "Pick", "answer_type": "single_choice", "options": "a:Alpha|b:Beta"},
        ])
        probes = ChecklistItem.objects.get(question="Probes ok?")
        assert probes.metadata == {"points": 100, "critical": True, "applies_to": ["EGP", "KSA"]}


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestWorkbookChecklists:
    """The two source workbooks (Spot Visit, RFSA), loaded through the services."""

    @pytest.fixture
    def loaded(self, org_a):
        call_command("seed_audit_checklists", organization=org_a.slug)
        return {t.code: t for t in ChecklistTemplate.objects.filter(organization=org_a)}

    def items(self, template):
        return ChecklistItem.objects.filter(section__version__template=template).select_related("section")

    def test_both_checklists_are_created_published_and_active(self, loaded):
        assert set(loaded) == {"FS-SPOT", "FS-RFSA"}
        for template in loaded.values():
            assert template.status == "active"
            version = template.versions.get()
            assert version.status == "published" and not version.is_draft

    def test_spot_visit_matches_the_workbook(self, loaded):
        template = loaded["FS-SPOT"]
        sections = list(template.versions.get().sections.order_by("sort_order").values_list("name", flat=True))
        assert sections == ["Visit details", "Stock Areas Check", "Food Safety Checks", "Cleaning and Hygiene",
                            "Crew Hygiene and Cleaning", "Store Pest Management Controls", "Documentation Control", "Sign-off"]
        scored = [i for i in self.items(template) if i.metadata.get("points")]
        assert len(scored) == 45
        assert sum(i.metadata["points"] for i in scored) == 4100  # the workbook's "possible" total
        assert sum(1 for i in scored if i.metadata.get("critical")) == 8  # the eight “/F” rows of the sheet
        stock = [i.metadata["points"] for i in self.items(template).filter(section__name="Stock Areas Check").order_by("sort_order")]
        assert sum(stock) == 650

    def test_rfsa_matches_the_workbook(self, loaded):
        template = loaded["FS-RFSA"]
        items = self.items(template)
        assert items.count() == 244
        assert items.filter(answer_type="matrix").count() == 11  # per-unit rows (freezers, grill-side, fryer vats)
        assert items.filter(metadata__critical=True).count() >= 30
        assert items.filter(metadata__applies_to__contains="KSA").exists()
        first = items.filter(section__name="Calibration Kit").order_by("sort_order")
        assert [i.metadata.get("points") for i in first] == [40, 100, 60]
        assert [i.metadata.get("critical") for i in first] == [None, True, None]

    def test_every_loaded_item_validates_as_an_answerable_item(self, loaded):
        for template in loaded.values():
            for item in ChecklistItem.objects.filter(section__version__template=template).prefetch_related("options"):
                assert item.answer_type in ("yes_no_na", "matrix", "text", "date", "numeric", "signature", "long_text"), item.answer_type
                result = validate_answer(item, None, required=False)
                assert result.status == "empty"

    def test_scored_items_can_be_answered_including_na_and_matrix(self, loaded):
        item = self.items(loaded["FS-RFSA"]).get(section__name__startswith="Freezers and Refrigerators - Secondary", sort_order=1)
        assert item.answer_type == "matrix"
        keys = [r["key"] for r in item.config["rows"]]
        assert keys == ["secondary", "back_up", "reach_in"]
        assert validate_answer(item, {"secondary": "compliant", "reach_in": "non_compliant"}).valid
        assert validate_answer(item, {"garage": "compliant"}).status == INVALID
        plain = self.items(loaded["FS-SPOT"]).filter(answer_type="yes_no_na").first()
        assert validate_answer(plain, "na").valid and validate_answer(plain, "maybe").status == INVALID

    def test_visit_details_and_signatures(self, loaded):
        spot = self.items(loaded["FS-SPOT"])
        assert spot.get(code="SPOT-VD-03").answer_type == "date"
        assert spot.get(code="SPOT-VD-08").config["unit"] == "%"
        signature = spot.get(code="SPOT-SO-02")
        assert signature.answer_type == "signature" and signature.config["signer_label"] == "Auditor"
        assert validate_answer(signature, {"signer_name": "H. Abbas"}).valid

    def test_loading_twice_changes_nothing(self, org_a, loaded):
        before = ChecklistItem.objects.count()
        call_command("seed_audit_checklists", organization=org_a.slug)
        assert ChecklistItem.objects.count() == before and ChecklistTemplate.objects.filter(organization=org_a).count() == 2

    def test_draft_option_and_single_selection(self, org_a):
        call_command("seed_audit_checklists", organization=org_a.slug, only="spot-visit", draft=True)
        template = ChecklistTemplate.objects.get(organization=org_a, code="FS-SPOT")
        assert template.status == "draft" and template.versions.get().is_draft
        assert not ChecklistTemplate.objects.filter(code="FS-RFSA").exists()

    def test_seed_files_are_clean_data(self):
        from apps.checklists import seed

        for name in ("spot_visit.json", "rfsa.json"):
            data_ = seed.load_data_file(name)
            codes = [i["code"] for s in data_["sections"] for i in s["items"]]
            assert len(codes) == len(set(codes))  # item codes are unique across the whole checklist
            text = json.dumps(data_, ensure_ascii=False)
            assert "Â" not in text and "â€" not in text  # no mojibake from the spreadsheet

    def test_loaded_template_is_visible_in_the_ui(self, client, org_a, loaded, make_user, assign):
        user = make_user(email="wb@example.com", organization=org_a, role=None)
        assign(user, org_a, "administrator")
        client.force_login(user)
        html = body(client.get(reverse("checklists:list")))
        assert "Restaurant Food Safety Spot Visit" in html and "Restaurant Food Safety Audit (RFSA)" in html
        page = body(client.get(reverse("checklists:detail", args=[loaded["FS-SPOT"].pk])))
        assert "Stock Areas Check" in page and "pts" in page and "Critical" in page
        rfsa = body(client.get(reverse("checklists:detail", args=[loaded["FS-RFSA"].pk])))
        assert "Rows: Secondary · Back-up · Reach-in" in rfsa
