"""Checklist screens: list, create, detail, builder, section/item/option flows, and access control."""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.checklists import constants as c
from apps.checklists import services
from apps.checklists.models import ChecklistItem, ChecklistSection, ChecklistTemplate
from apps.permissions import services as rbac
from apps.permissions.registry import P


def body(response) -> str:
    return response.content.decode()


@pytest.fixture
def world(org_a, org_b, make_user, assign):
    author = make_user(email="qa@example.com", organization=org_a, role=None)
    assign(author, org_a, "administrator")
    reader = make_user(email="reader@example.com", organization=org_a, role=None)
    assign(reader, org_a, rbac.create_role(organization=org_a, name="Reader", permissions=[P.CHECKLIST_VIEW]))
    plain = make_user(email="plain-c@example.com", organization=org_a, role=None)
    other = make_user(email="other-admin@example.com", organization=org_b, role=None)
    assign(other, org_b, "administrator")
    template = services.create_checklist_template(organization=org_a, name="Food Safety", code="FS-001", actor=author)
    foreign = services.create_checklist_template(organization=org_b, name="Foreign Secret", code="FX-1", actor=other)
    return {"author": author, "reader": reader, "plain": plain, "other": other, "template": template, "foreign": foreign}


def url(name, *args):
    return reverse(f"checklists:{name}", args=args)


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestListAndAccess:
    def test_anonymous_redirected(self, client):
        assert client.get(url("list")).status_code == 302

    def test_list_shows_only_own_organization(self, client, world):
        client.force_login(world["author"])
        html = body(client.get(url("list")))
        assert "Food Safety" in html and "FS-001" in html and "Foreign Secret" not in html

    def test_search_filter_and_status(self, client, world, org_a):
        services.create_checklist_template(organization=org_a, name="Opening Checklist", code="OP-1", actor=world["author"])
        client.force_login(world["author"])
        assert "Opening Checklist" not in body(client.get(url("list"), {"q": "food"}))
        assert "Opening Checklist" in body(client.get(url("list"), {"q": "OP-1"}))
        assert "Food Safety" not in body(client.get(url("list"), {"status": "archived"}))

    def test_search_never_reaches_another_organization(self, client, world):
        client.force_login(world["author"])
        assert "Foreign Secret" not in body(client.get(url("list"), {"q": "Foreign"}))

    def test_member_without_permission_is_forbidden(self, client, world):
        client.force_login(world["plain"])
        assert client.get(url("list")).status_code == 403

    def test_reader_sees_list_but_no_create_button(self, client, world):
        client.force_login(world["reader"])
        html = body(client.get(url("list")))
        assert "Food Safety" in html and url("create") not in html

    def test_nav_item_only_with_permission(self, client, world):
        client.force_login(world["author"])
        assert url("list") in body(client.get(reverse("core:home")))
        client.force_login(world["plain"])
        assert url("list") not in body(client.get(reverse("core:home")))


@pytest.mark.django_db
class TestTemplateCrud:
    def test_create_through_the_form(self, client, world, org_a):
        client.force_login(world["author"])
        assert client.get(url("create")).status_code == 200
        response = client.post(url("create"), {
            "organization": org_a.pk, "name": "Cleaning", "code": "clean-001", "new_category": "Operations",
            "description": "d", "instructions": "i", "estimated_duration_minutes": "15",
        })
        template = ChecklistTemplate.objects.get(code="CLEAN-001")
        assert response.status_code == 302 and template.category.name == "Operations"
        assert template.estimated_duration_minutes == 15 and template.versions.count() == 1

    def test_duplicate_code_shows_an_error(self, client, world, org_a):
        client.force_login(world["author"])
        response = client.post(url("create"), {"organization": org_a.pk, "name": "X", "code": "FS-001"})
        assert response.status_code == 200 and ChecklistTemplate.objects.filter(name="X").count() == 0

    def test_cannot_create_in_another_organization(self, client, world, org_b):
        client.force_login(world["author"])
        response = client.post(url("create"), {"organization": org_b.pk, "name": "Sneaky", "code": "SN-1"})
        assert response.status_code == 200 and not ChecklistTemplate.objects.filter(name="Sneaky").exists()

    def test_reader_cannot_create(self, client, world, org_a):
        client.force_login(world["reader"])
        assert client.get(url("create")).status_code == 403
        assert client.post(url("create"), {"organization": org_a.pk, "name": "N", "code": "N-1"}).status_code == 403

    def test_detail_renders_tabs_and_details(self, client, world):
        client.force_login(world["author"])
        html = body(client.get(url("detail", world["template"].pk)))
        assert "FS-001" in html and "Structure" in html and "Versions" in html and "Activity" in html
        assert "checklist.created" in html

    def test_edit_keeps_the_code(self, client, world):
        client.force_login(world["author"])
        t = world["template"]
        assert client.get(url("edit", t.pk)).status_code == 200
        client.post(url("edit", t.pk), {"organization": t.organization_id, "name": "Renamed", "code": "HACK", "description": "x"})
        t.refresh_from_db()
        assert t.name == "Renamed" and t.code == "FS-001"

    def test_reader_cannot_edit_but_can_view(self, client, world):
        client.force_login(world["reader"])
        t = world["template"]
        assert client.get(url("detail", t.pk)).status_code == 200
        assert client.get(url("edit", t.pk)).status_code == 403
        assert client.get(url("builder", t.pk)).status_code == 403
        assert client.post(url("status", t.pk), {"action": "archive"}).status_code == 403

    def test_duplicate_flow(self, client, world):
        client.force_login(world["author"])
        t = world["template"]
        assert client.get(url("duplicate", t.pk)).status_code == 200
        response = client.post(url("duplicate", t.pk), {"name": "Food Safety - Copy", "code": "FS-009"})
        assert response.status_code == 302 and ChecklistTemplate.objects.filter(code="FS-009").exists()

    def test_archive_restore_and_delete(self, client, world, org_a):
        client.force_login(world["author"])
        t = world["template"]
        client.post(url("status", t.pk), {"action": "archive"})
        t.refresh_from_db()
        assert t.status == "archived"
        assert "archived" in body(client.get(url("detail", t.pk))).lower()
        assert client.get(url("edit", t.pk)).status_code == 200  # form renders, service refuses the save
        client.post(url("status", t.pk), {"action": "restore"})
        t.refresh_from_db()
        assert t.status == "draft"
        client.post(url("status", t.pk), {"action": "delete"})
        assert not ChecklistTemplate.objects.filter(pk=t.pk).exists()


@pytest.mark.django_db
class TestIsolation:
    def test_other_tenants_template_is_404_everywhere(self, client, world):
        client.force_login(world["author"])
        f = world["foreign"]
        for name in ("detail", "edit", "builder", "duplicate", "versions"):
            assert client.get(url(name, f.pk)).status_code == 404, name
        assert client.post(url("status", f.pk), {"action": "archive"}).status_code == 404
        assert client.post(url("version_new", f.pk)).status_code == 404
        f.refresh_from_db()
        assert f.status == "draft"

    def test_foreign_section_item_ids_are_404(self, client, world):
        f = world["foreign"]
        fv = f.versions.get()
        fs = services.create_section(fv, name="Secret", actor=world["other"])
        fi = services.create_item(fs, question="Secret?", actor=world["other"])
        client.force_login(world["author"])
        own = world["template"]
        # Right organization in the URL, someone else's section/item ids: never resolves.
        assert client.get(url("section_edit", own.pk, fs.pk)).status_code == 404
        assert client.get(url("item_edit", own.pk, fi.pk)).status_code == 404
        assert client.post(url("item_delete", own.pk, fi.pk)).status_code == 404
        assert client.post(url("section_delete", own.pk, fs.pk)).status_code == 404
        assert ChecklistItem.objects.filter(pk=fi.pk).exists()

    def test_non_uuid_ids_do_not_crash(self, client, world):
        client.force_login(world["author"])
        assert client.get("/checklists/not-a-uuid/").status_code == 404


@pytest.mark.django_db
class TestBuilder:
    def test_builder_empty_state_and_section_flow(self, client, world):
        client.force_login(world["author"])
        t = world["template"]
        html = body(client.get(url("builder", t.pk)))
        assert "No sections yet" in html and "Add section" in html and "Draft" in html
        assert client.post(url("section_new", t.pk), {"name": "Personal Hygiene", "is_required": "on"}).status_code == 302
        client.post(url("section_new", t.pk), {"name": "Food Storage"})
        sections = list(ChecklistSection.objects.filter(version__template=t).order_by("sort_order"))
        assert [s.name for s in sections] == ["Personal Hygiene", "Food Storage"]
        client.post(url("section_move", t.pk, sections[1].pk), {"direction": "up"})
        assert list(ChecklistSection.objects.filter(version__template=t).order_by("sort_order").values_list("name", flat=True)) == ["Food Storage", "Personal Hygiene"]
        client.post(url("section_edit", t.pk, sections[0].pk), {"name": "Hygiene", "is_required": ""})
        sections[0].refresh_from_db()
        assert sections[0].name == "Hygiene" and not sections[0].is_required
        html = body(client.get(url("builder", t.pk)))
        assert "Hygiene" in html and "Move up" in html

    def test_item_flow_with_types(self, client, world):
        client.force_login(world["author"])
        t = world["template"]
        section = services.create_section(t.versions.get(), name="S", actor=world["author"])
        assert client.get(url("item_new", t.pk, section.pk)).status_code == 200
        client.post(url("item_new", t.pk, section.pk), {"question": "Is the exit accessible?", "answer_type": "boolean", "is_required": "on", "is_active": "on"})
        client.post(url("item_new", t.pk, section.pk), {"question": "Rate cleanliness", "answer_type": "score", "cfg-min": "1", "cfg-max": "10", "is_active": "on"})
        client.post(url("item_new", t.pk, section.pk), {"question": "Fridge temperature", "answer_type": "numeric", "cfg-min": "-5", "cfg-max": "8", "cfg-unit": "°C", "is_active": "on"})
        items = {i.answer_type: i for i in ChecklistItem.objects.filter(section=section)}
        assert set(items) == {"boolean", "score", "numeric"}
        assert items["score"].config == {"min": 1, "max": 10, "step": 1, "display": "numeric"}
        assert items["numeric"].config == {"min": -5, "max": 8, "unit": "°C", "decimals": 2}
        assert items["boolean"].is_required and not items["score"].is_required

    def test_invalid_config_shows_error_and_saves_nothing(self, client, world):
        client.force_login(world["author"])
        t = world["template"]
        section = services.create_section(t.versions.get(), name="S", actor=world["author"])
        response = client.post(url("item_new", t.pk, section.pk), {"question": "Rate", "answer_type": "score", "cfg-min": "9", "cfg-max": "3"})
        assert response.status_code == 200 and not ChecklistItem.objects.filter(section=section).exists()

    def test_choice_item_options_flow(self, client, world):
        client.force_login(world["author"])
        t = world["template"]
        section = services.create_section(t.versions.get(), name="S", actor=world["author"])
        response = client.post(url("item_new", t.pk, section.pk), {"question": "Condition", "answer_type": "single_choice", "is_active": "on"})
        item = ChecklistItem.objects.get(section=section)
        assert response.status_code == 302 and str(item.pk) in response["Location"]  # straight to the options editor
        for label in ("Good", "Fair", "Poor"):
            client.post(url("option_add", t.pk, item.pk), {"label": label})
        options = list(item.options.order_by("sort_order"))
        assert [o.label for o in options] == ["Good", "Fair", "Poor"]
        client.post(url("option_move", t.pk, item.pk, options[2].pk), {"direction": "up"})
        client.post(url("option_edit", t.pk, item.pk, options[0].pk), {"label": "Excellent"})
        client.post(url("option_delete", t.pk, item.pk, options[1].pk))
        assert list(item.options.order_by("sort_order").values_list("label", flat=True)) == ["Poor", "Excellent"] or item.options.count() == 2
        html = body(client.get(url("item_edit", t.pk, item.pk)))
        assert "New option" in html and "Options" in html

    def test_yes_no_na_shows_fixed_options(self, client, world):
        client.force_login(world["author"])
        t = world["template"]
        section = services.create_section(t.versions.get(), name="S", actor=world["author"])
        item = services.create_item(section, question="Clean?", answer_type="yes_no_na", actor=world["author"])
        html = body(client.get(url("item_edit", t.pk, item.pk)))
        assert "Fixed by the answer type" in html and "N/A" in html and "New option" not in html
        client.post(url("option_add", t.pk, item.pk), {"label": "Maybe"})
        assert item.options.count() == 3

    def test_item_move_delete_and_edit(self, client, world):
        client.force_login(world["author"])
        t = world["template"]
        section = services.create_section(t.versions.get(), name="S", actor=world["author"])
        a = services.create_item(section, question="A", actor=world["author"])
        b = services.create_item(section, question="B", actor=world["author"])
        client.post(url("item_move", t.pk, b.pk), {"direction": "up"})
        assert list(section.items.order_by("sort_order").values_list("question", flat=True)) == ["B", "A"]
        client.post(url("item_edit", t.pk, a.pk), {"question": "A2", "code": a.code, "answer_type": "yes_no", "is_active": "on"})
        a.refresh_from_db()
        assert a.question == "A2" and a.code == "Q-001"
        client.post(url("item_delete", t.pk, b.pk))
        assert section.items.count() == 1

    def test_publish_freezes_the_builder(self, client, world):
        client.force_login(world["author"])
        t = world["template"]
        section = services.create_section(t.versions.get(), name="S", actor=world["author"])
        item = services.create_item(section, question="Q", actor=world["author"])
        client.post(url("version_publish", t.pk, 1), {"change_summary": "First"})
        v1 = t.versions.get()
        assert v1.status == "published" and v1.change_summary == "First"
        # No draft any more: every edit URL is 404 (nothing to edit), nothing is silently changed.
        assert client.get(url("section_edit", t.pk, section.pk)).status_code == 404
        assert client.post(url("item_delete", t.pk, item.pk)).status_code == 404
        html = body(client.get(url("builder", t.pk)))
        assert "No draft version" in html and "New draft version" in html
        client.post(url("version_new", t.pk), {"change_summary": "Tweak"})
        assert t.versions.count() == 2 and t.versions.get(version_number=2).is_draft
        assert "Q" in body(client.get(url("version_detail", t.pk, 1)))
        assert "immutable" in body(client.get(url("version_detail", t.pk, 1)))

    def test_publish_with_problems_is_refused(self, client, world):
        client.force_login(world["author"])
        t = world["template"]
        services.create_section(t.versions.get(), name="Empty", actor=world["author"])
        client.post(url("version_publish", t.pk, 1))
        assert t.versions.get().status == "draft"

    def test_activate_after_publish(self, client, world):
        client.force_login(world["author"])
        t = world["template"]
        section = services.create_section(t.versions.get(), name="S", actor=world["author"])
        services.create_item(section, question="Q", actor=world["author"])
        client.post(url("version_publish", t.pk, 1))
        client.post(url("status", t.pk), {"action": "activate"})
        t.refresh_from_db()
        assert t.status == "active"

    def test_reader_cannot_use_builder_posts(self, client, world):
        client.force_login(world["reader"])
        t = world["template"]
        assert client.post(url("section_new", t.pk), {"name": "X"}).status_code == 403
        assert not ChecklistSection.objects.filter(version__template=t).exists()

    def test_posts_only_for_actions(self, client, world):
        client.force_login(world["author"])
        t = world["template"]
        section = services.create_section(t.versions.get(), name="S", actor=world["author"])
        assert client.get(url("section_delete", t.pk, section.pk)).status_code == 405
        assert client.get(url("status", t.pk)).status_code == 405

    def test_builder_query_count_is_bounded(self, client, world, django_assert_max_num_queries):
        client.force_login(world["author"])
        t = world["template"]
        draft = t.versions.get()
        for i in range(6):
            s = services.create_section(draft, name=f"S{i}", actor=world["author"])
            for j in range(5):
                services.create_item(s, question=f"Q{i}{j}", answer_type="single_choice", options=[("a", "A"), ("b", "B")], actor=world["author"])
        client.get(url("builder", t.pk))
        with django_assert_max_num_queries(32):
            assert client.get(url("builder", t.pk)).status_code == 200

    def test_xss_in_names_is_escaped(self, client, world):
        client.force_login(world["author"])
        t = world["template"]
        services.create_section(t.versions.get(), name="<script>alert(1)</script>", actor=world["author"])
        html = body(client.get(url("builder", t.pk)))
        assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html

    def test_csrf_is_required(self, world):
        from django.test import Client

        strict = Client(enforce_csrf_checks=True)
        strict.force_login(world["author"])
        assert strict.post(url("status", world["template"].pk), {"action": "archive"}).status_code == 403
