"""Checklist API, import hooks, admin guards and the demo seed - all through the shared authorization."""

from __future__ import annotations

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APIClient

from apps.checklists import imports, selectors, services
from apps.checklists.models import ChecklistItem, ChecklistTemplate
from apps.permissions import services as rbac
from apps.permissions.registry import P


def api(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


def data(response):
    payload = response.json()
    return payload.get("data", payload) if isinstance(payload, dict) else payload


@pytest.fixture
def world(org_a, org_b, make_user, assign):
    author = make_user(email="api-author@example.com", organization=org_a, role=None)
    assign(author, org_a, "administrator")
    reader = make_user(email="api-reader@example.com", organization=org_a, role=None)
    assign(reader, org_a, rbac.create_role(organization=org_a, name="Reader", permissions=[P.CHECKLIST_VIEW]))
    plain = make_user(email="api-plain@example.com", organization=org_a, role=None)
    other = make_user(email="api-other@example.com", organization=org_b, role=None)
    assign(other, org_b, "administrator")
    template = services.create_checklist_template(organization=org_a, name="Food Safety", code="FS-001", actor=author)
    section = services.create_section(template.versions.get(), name="Storage", actor=author)
    services.create_item(section, question="Fridge temp", answer_type="numeric", config={"min": 0, "max": 8}, actor=author)
    services.create_item(section, question="Condition", answer_type="single_choice", options=[("g", "Good"), ("p", "Poor")], actor=author)
    foreign = services.create_checklist_template(organization=org_b, name="Foreign", code="FX-1", actor=other)
    return {"author": author, "reader": reader, "plain": plain, "other": other, "template": template, "foreign": foreign}


@pytest.mark.django_db
class TestChecklistAPI:
    def test_unauthenticated(self):
        assert APIClient().get("/api/v1/checklists/").status_code in (401, 403)

    def test_list_is_scoped_to_the_organization(self, world):
        rows = data(api(world["author"]).get("/api/v1/checklists/"))
        assert {r["code"] for r in rows} == {"FS-001"}

    def test_member_without_permission_sees_nothing(self, world):
        assert data(api(world["plain"]).get("/api/v1/checklists/")) == []

    def test_search_and_filter(self, world, org_a):
        services.create_checklist_template(organization=org_a, name="Opening", code="OP-1", actor=world["author"])
        client = api(world["author"])
        assert {r["code"] for r in data(client.get("/api/v1/checklists/", {"search": "open"}))} == {"OP-1"}
        assert {r["code"] for r in data(client.get("/api/v1/checklists/", {"status": "draft", "q": "food"}))} == {"FS-001"}
        assert {r["code"] for r in data(client.get("/api/v1/checklists/", {"search": "Foreign"}))} == set()

    def test_retrieve_foreign_is_404(self, world):
        assert api(world["author"]).get(f"/api/v1/checklists/{world['foreign'].pk}/").status_code == 404
        assert api(world["author"]).get(f"/api/v1/checklists/{world['template'].pk}/").status_code == 200

    def test_create_update_and_archive(self, world, org_a):
        client = api(world["author"])
        created = client.post("/api/v1/checklists/", {"organization": str(org_a.pk), "code": "cl-1", "name": "Cleaning"}, format="json")
        assert created.status_code == 201
        body = data(created)
        assert body["code"] == "CL-1" and body["status"] == "draft" and body["latest_version"] == 1
        patched = client.patch(f"/api/v1/checklists/{body['id']}/", {"name": "Cleaning 2", "code": "HACK"}, format="json")
        assert data(patched)["name"] == "Cleaning 2" and data(patched)["code"] == "CL-1"
        assert data(client.post(f"/api/v1/checklists/{body['id']}/archive/"))["status"] == "archived"
        assert data(client.post(f"/api/v1/checklists/{body['id']}/restore/"))["status"] == "draft"

    def test_create_validation(self, world, org_a):
        client = api(world["author"])
        assert client.post("/api/v1/checklists/", {"name": "x"}, format="json").status_code == 400
        assert client.post("/api/v1/checklists/", {"organization": str(org_a.pk), "code": "FS-001", "name": "Dup"}, format="json").status_code == 400

    def test_create_in_other_organization(self, world, org_b):
        client = api(world["author"])
        assert client.post("/api/v1/checklists/", {"organization": str(org_b.pk), "code": "Z-1", "name": "Z"}, format="json").status_code == 404
        assert not ChecklistTemplate.objects.filter(code="Z-1").exists()

    def test_reader_can_read_but_not_write(self, world, org_a):
        client = api(world["reader"])
        pk = world["template"].pk
        assert client.get(f"/api/v1/checklists/{pk}/").status_code == 200
        assert client.post("/api/v1/checklists/", {"organization": str(org_a.pk), "code": "R-1", "name": "R"}, format="json").status_code == 403
        assert client.patch(f"/api/v1/checklists/{pk}/", {"name": "Hacked"}, format="json").status_code == 403
        assert client.post(f"/api/v1/checklists/{pk}/archive/").status_code == 403
        assert client.post(f"/api/v1/checklists/{pk}/duplicate/").status_code == 403
        assert client.post(f"/api/v1/checklists/{pk}/versions/new/").status_code == 403
        world["template"].refresh_from_db()
        assert world["template"].name == "Food Safety"

    def test_foreign_writes_are_404(self, world):
        client = api(world["author"])
        pk = world["foreign"].pk
        assert client.patch(f"/api/v1/checklists/{pk}/", {"name": "H"}, format="json").status_code == 404
        assert client.post(f"/api/v1/checklists/{pk}/archive/").status_code == 404
        assert client.get(f"/api/v1/checklists/{pk}/sections/").status_code == 404

    def test_duplicate(self, world):
        response = api(world["author"]).post(f"/api/v1/checklists/{world['template'].pk}/duplicate/", {}, format="json")
        assert response.status_code == 201 and data(response)["code"] == "FS-001-COPY"

    def test_structure_endpoints(self, world):
        client = api(world["author"])
        pk = world["template"].pk
        sections = data(client.get(f"/api/v1/checklists/{pk}/sections/"))
        assert [s["name"] for s in sections] == ["Storage"]
        assert [i["answer_type"] for i in sections[0]["items"]] == ["numeric", "single_choice"]
        assert [o["label"] for o in sections[0]["items"][1]["options"]] == ["Good", "Poor"]
        items = data(client.get(f"/api/v1/checklists/{pk}/items/"))
        assert [i["code"] for i in items] == ["Q-001", "Q-002"] and items[0]["config"] == {"min": 0, "max": 8, "decimals": 2}
        assert client.get(f"/api/v1/checklists/{pk}/items/", {"version": "x"}).status_code == 400
        assert client.get(f"/api/v1/checklists/{pk}/items/", {"version": "9"}).status_code == 404
        versions = data(client.get(f"/api/v1/checklists/{pk}/versions/"))
        assert versions[0]["version_number"] == 1 and versions[0]["status"] == "draft"

    def test_new_version_requires_published_one(self, world):
        client = api(world["author"])
        pk = world["template"].pk
        assert client.post(f"/api/v1/checklists/{pk}/versions/new/", {}, format="json").status_code == 400  # draft exists
        services.publish_checklist_version(world["template"].versions.get(), actor=world["author"])
        response = client.post(f"/api/v1/checklists/{pk}/versions/new/", {"change_summary": "v2"}, format="json")
        assert response.status_code == 201 and data(response)["version_number"] == 2

    def test_no_delete_endpoint(self, world):
        assert api(world["author"]).delete(f"/api/v1/checklists/{world['template'].pk}/").status_code == 405

    def test_list_query_count_is_flat(self, world, org_a, django_assert_max_num_queries):
        for i in range(15):
            services.create_checklist_template(organization=org_a, name=f"T{i}", code=f"T-{i}", actor=world["author"])
        client = api(world["author"])
        client.get("/api/v1/checklists/")
        with django_assert_max_num_queries(14):
            client.get("/api/v1/checklists/")


@pytest.mark.django_db
class TestImportBoundary:
    ROWS = [
        {"template": "FS-001", "section": "Storage", "question": "Is it labelled?", "answer_type": "yes_no_na", "code": "LBL-1"},
        {"template": "fs-001", "section": "Hygiene", "question": "Rate it", "answer_type": "score", "config": '{"min": 1, "max": 10}'},
        {"template": "FS-001", "section": "Hygiene", "question": "Pick", "answer_type": "single_choice", "options": "a:Alpha|b:Beta"},
    ]

    def test_import_creates_sections_and_items(self, world, org_a):
        result = imports.import_items(user=world["author"], organization=org_a, rows=self.ROWS)
        assert result == {"items": 3, "sections": 2}
        assert ChecklistItem.objects.filter(code="LBL-1", section__name="Storage").exists()
        pick = ChecklistItem.objects.get(question="Pick")
        assert list(pick.options.order_by("sort_order").values_list("value", flat=True)) == ["a", "b"]

    def test_a_bad_row_rolls_everything_back(self, world, org_a):
        before = ChecklistItem.objects.count()
        rows = [*self.ROWS, {"template": "FS-001", "section": "X", "question": "Bad", "answer_type": "telepathy"}]
        with pytest.raises(ValidationError) as info:
            imports.import_items(user=world["author"], organization=org_a, rows=rows)
        assert "Row 5" in str(info.value.messages)
        assert ChecklistItem.objects.count() == before
        assert not world["template"].versions.get().sections.filter(name="Hygiene").exists()

    def test_missing_columns_and_bad_config(self, world, org_a):
        with pytest.raises(ValidationError):
            imports.import_items(user=world["author"], organization=org_a, rows=[{"template": "FS-001", "section": "S"}])
        with pytest.raises(ValidationError):
            imports.import_items(user=world["author"], organization=org_a, rows=[
                {"template": "FS-001", "section": "S", "question": "Q", "answer_type": "score", "config": "{not json"}])

    def test_cannot_import_into_another_organization(self, world, org_a, org_b):
        with pytest.raises(PermissionDenied):
            imports.import_items(user=world["author"], organization=org_b, rows=self.ROWS)
        with pytest.raises(ValidationError) as info:  # a foreign template code looks exactly like an unknown one
            imports.import_items(user=world["author"], organization=org_a,
                                 rows=[{"template": "FX-1", "section": "S", "question": "Q"}])
        assert "not found" in str(info.value.messages)
        assert not ChecklistItem.objects.filter(question="Q").exists()

    def test_reader_cannot_import(self, world, org_a):
        with pytest.raises(PermissionDenied):
            imports.import_items(user=world["reader"], organization=org_a, rows=self.ROWS)

    def test_published_version_cannot_be_imported_into(self, world, org_a):
        services.publish_checklist_version(world["template"].versions.get(), actor=world["author"])
        with pytest.raises(ValidationError):
            imports.import_items(user=world["author"], organization=org_a, rows=self.ROWS)


@pytest.mark.django_db
class TestAdminAndSeed:
    def test_export_queryset_is_authorized(self, world, rf):
        from django.contrib import admin

        model_admin = admin.site._registry[ChecklistTemplate]
        request = rf.get("/")
        request.user = world["author"]
        assert {t.code for t in model_admin.get_export_queryset(request)} == {"FS-001"}

    def test_published_structure_is_read_only_in_admin(self, world, rf, superuser):
        from django.contrib import admin

        from apps.checklists.models import ChecklistSection

        services.create_item(world["template"].versions.get().sections.first(), question="Q3", actor=world["author"])
        section = ChecklistSection.objects.filter(version__template=world["template"]).first()
        model_admin = admin.site._registry[ChecklistSection]
        request = rf.get("/")
        request.user = superuser
        assert model_admin.has_change_permission(request, section)
        services.publish_checklist_version(world["template"].versions.get(), actor=world["author"])
        section.refresh_from_db()
        assert not model_admin.has_change_permission(request, section)
        assert not model_admin.has_delete_permission(request, section)
        assert not admin.site._registry[ChecklistTemplate].has_delete_permission(request, world["template"])

    def test_seed_demo_is_idempotent_and_generic(self, org_a):
        from django.core.management import call_command

        call_command("seed_demo_checklists", organization=org_a.slug)
        call_command("seed_demo_checklists", organization=org_a.slug)
        assert ChecklistTemplate.objects.filter(organization=org_a, code__startswith="DEMO-").count() == 4
        types = set(ChecklistItem.objects.filter(section__version__template__organization=org_a).values_list("answer_type", flat=True))
        assert {"yes_no", "yes_no_na", "score", "numeric", "long_text", "boolean", "single_choice", "multiple_choice", "photo"} <= types
        assert ChecklistTemplate.objects.get(code="DEMO-FSI").status == "active"
        from apps.locations.models import Location

        assert Location.objects.filter(organization=org_a, name="McDonald's Demo Restaurant", location_type__code="RESTAURANT").count() == 1
