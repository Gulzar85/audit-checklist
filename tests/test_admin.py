"""Django admin: safe management of organizations, location types and locations."""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.core.models import ActivityLog
from apps.locations import services
from apps.locations.models import Location, LocationType
from apps.organizations.models import Organization

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client(client, superuser):
    client.force_login(superuser)
    return client


#: The organization admin embeds a members inline; every POST must carry its management form.
MEMBERS_FORMSET = {
    "memberships-TOTAL_FORMS": "0",
    "memberships-INITIAL_FORMS": "0",
    "memberships-MIN_NUM_FORMS": "0",
    "memberships-MAX_NUM_FORMS": "1000",
}


def add_url(model: str) -> str:
    app = "organizations" if model == "organization" else "locations"
    return reverse(f"admin:{app}_{model}_add")


def change_url(model: str, obj) -> str:
    app = "organizations" if model == "organization" else "locations"
    return reverse(f"admin:{app}_{model}_change", args=[obj.pk])


def changelist(model: str) -> str:
    app = "organizations" if model == "organization" else "locations"
    return reverse(f"admin:{app}_{model}_changelist")


class TestAccess:
    @pytest.mark.parametrize("model", ["organization", "locationtype", "location"])
    def test_non_staff_cannot_reach_the_admin(self, client, admin_a, model):
        client.force_login(admin_a)
        assert client.get(changelist(model)).status_code == 302

    @pytest.mark.parametrize("model", ["organization", "locationtype", "location"])
    def test_pages_render_for_staff(self, admin_client, model, org_a, make_type, make_location):
        make_location(org_a, "L")
        for url in (changelist(model), add_url(model)):
            assert admin_client.get(url).status_code == 200

    def test_change_pages_render(self, admin_client, org_a, make_type, make_location):
        location = make_location(org_a, "L")
        assert admin_client.get(change_url("organization", org_a)).status_code == 200
        assert admin_client.get(change_url("locationtype", location.location_type)).status_code == 200
        assert admin_client.get(change_url("location", location)).status_code == 200

    def test_history_pages_render(self, admin_client, org_a, make_location):
        location = make_location(org_a, "L")
        assert admin_client.get(reverse("admin:locations_location_history", args=[location.pk])).status_code == 200
        assert admin_client.get(reverse("admin:organizations_organization_history", args=[org_a.pk])).status_code == 200

    def test_changelists_support_search_and_filters(self, admin_client, org_a, org_b, make_location):
        make_location(org_a, "Findable", code="FIND")
        make_location(org_b, "Other", code="OTHER")
        body = admin_client.get(changelist("location"), {"q": "findable"}).content.decode()
        assert "Findable" in body and "OTHER" not in body
        assert admin_client.get(changelist("location"), {"organization__id__exact": str(org_a.pk), "is_active__exact": "1"}).status_code == 200
        assert admin_client.get(changelist("organization"), {"status__exact": "active"}).status_code == 200
        assert admin_client.get(changelist("locationtype"), {"q": "node"}).status_code == 200

    def test_changelists_have_import_and_export(self, admin_client):
        for model in ("organization", "locationtype", "location"):
            body = admin_client.get(changelist(model)).content.decode()
            assert "import" in body.lower() and "export" in body.lower()


class TestOrganizationAdmin:
    def test_status_and_code_are_read_only_on_the_change_form(self, admin_client, org_a):
        body = admin_client.get(change_url("organization", org_a)).content.decode()
        assert 'name="status"' not in body and 'name="code"' not in body
        assert "Use the actions on the list page to change status" in body

    def test_code_can_be_chosen_when_creating(self, admin_client):
        body = admin_client.get(add_url("organization")).content.decode()
        assert 'name="code"' in body and 'name="status"' not in body

    def test_creating_in_the_admin_is_logged(self, admin_client, superuser):
        response = admin_client.post(add_url("organization"), {"name": "Admin Made", "slug": "admin-made", **MEMBERS_FORMSET, "_save": "Save"})
        assert response.status_code == 302
        org = Organization.objects.get(slug="admin-made")
        assert org.code == "ADMIN_MADE" and org.is_active
        entry = ActivityLog.objects.get(verb="organization.created")
        assert entry.actor == superuser and entry.metadata["source"] == "admin"

    def test_editing_is_logged_with_changed_fields(self, admin_client, org_a):
        data = {"name": "Renamed In Admin", "slug": org_a.slug, **MEMBERS_FORMSET, "_save": "Save"}
        assert admin_client.post(change_url("organization", org_a), data).status_code == 302
        entry = ActivityLog.objects.get(verb="organization.updated")
        assert "name" in entry.metadata["changed_fields"] and entry.metadata["source"] == "admin"

    def test_invalid_values_are_reported_in_the_form(self, admin_client):
        response = admin_client.post(add_url("organization"), {"name": "Bad", "slug": "bad", "timezone": "Nowhere/Land", **MEMBERS_FORMSET, "_save": "Save"})
        assert response.status_code == 200 and "recognised IANA time zone" in response.content.decode()
        assert not Organization.objects.filter(slug="bad").exists()

    def test_cannot_be_deleted(self, admin_client, org_a):
        url = reverse("admin:organizations_organization_delete", args=[org_a.pk])
        assert admin_client.get(url).status_code == 403
        assert admin_client.post(url, {"post": "yes"}).status_code == 403
        assert Organization.objects.filter(pk=org_a.pk).exists()

    @pytest.mark.parametrize(
        ("action", "status"),
        [("deactivate_selected", "inactive"), ("suspend_selected", "suspended")],
    )
    def test_status_actions_go_through_the_service_layer(self, admin_client, org_a, superuser, action, status):
        response = admin_client.post(
            changelist("organization"), {"action": action, "_selected_action": [str(org_a.pk)]}
        )
        assert response.status_code == 302
        org_a.refresh_from_db()
        assert org_a.status == status
        assert ActivityLog.objects.filter(organization=org_a, actor=superuser, metadata__to=status).exists()

    def test_activate_action(self, admin_client, org_a):
        from apps.organizations import services as org_services

        org_services.deactivate_organization(org_a)
        admin_client.post(changelist("organization"), {"action": "activate_selected", "_selected_action": [str(org_a.pk)]})
        org_a.refresh_from_db()
        assert org_a.is_active

    def test_branding_preview_uses_no_inline_style(self, admin_client, org_a, settings, tmp_path):
        import io

        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image

        from apps.organizations import services as org_services

        settings.MEDIA_ROOT = tmp_path
        buf = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buf, "PNG")
        org_services.update_organization(org_a, logo=SimpleUploadedFile("l.png", buf.getvalue(), "image/png"))
        body = admin_client.get(change_url("organization", org_a)).content.decode()
        assert 'alt="Logo"' in body and 'height="60"' in body


class TestLocationAdmin:
    def payload(self, org, location_type, **extra):
        return {
            "organization": str(org.pk), "location_type": str(location_type.pk),
            "name": "Admin Loc", "code": "adm-1", "is_active": "on", "metadata": "{}", "_save": "Save", **extra,
        }

    def test_create_with_a_valid_parent(self, admin_client, org_a, make_location):
        parent = make_location(org_a, "P")
        response = admin_client.post(add_url("location"), self.payload(org_a, parent.location_type, parent=str(parent.pk)))
        assert response.status_code == 302
        created = Location.objects.get(name="Admin Loc")
        assert created.parent == parent and created.code == "ADM-1"
        assert ActivityLog.objects.filter(verb="location.created", metadata__source="admin").exists()

    def test_cross_organization_parent_is_rejected(self, admin_client, org_a, org_b, make_location, make_type):
        foreign_parent = make_location(org_b, "Foreign")
        response = admin_client.post(
            add_url("location"), self.payload(org_a, make_type(org_a, "T", "T"), parent=str(foreign_parent.pk))
        )
        assert response.status_code == 200 and not Location.objects.filter(name="Admin Loc").exists()
        assert "valid choice" in response.content.decode() or "same organization" in response.content.decode()

    def test_cross_organization_type_is_rejected(self, admin_client, org_a, org_b, make_type):
        response = admin_client.post(add_url("location"), self.payload(org_a, make_type(org_b, "T", "T")))
        assert response.status_code == 200 and not Location.objects.filter(name="Admin Loc").exists()

    def test_form_only_offers_same_organization_choices(self, org_a, org_b, make_location, make_type):
        """The form's querysets (what it *validates* against) are scoped, not just its dropdowns."""
        from apps.locations.admin import LocationAdminForm

        mine = make_location(org_a, "Mine")
        other_mine = make_location(org_a, "Other Mine")
        foreign_parent = make_location(org_b, "Foreign Parent")
        foreign_type = make_type(org_b, "Foreign Type", "FT")
        platform = make_type(None, "Platform", "PLAT")
        form = LocationAdminForm(instance=mine)
        assert set(form.fields["parent"].queryset) == {other_mine}
        assert foreign_parent not in form.fields["parent"].queryset
        types = set(form.fields["location_type"].queryset)
        assert foreign_type not in types and platform in types and mine.location_type in types

    def test_a_descendant_is_not_offered_as_a_parent(self, chain_a):
        from apps.locations.admin import LocationAdminForm

        offered = set(LocationAdminForm(instance=chain_a[1]).fields["parent"].queryset)
        assert chain_a[0] in offered  # the ancestor is fine
        assert not offered & {chain_a[1], chain_a[2], chain_a[3], chain_a[4]}  # itself and its subtree are not

    def test_the_add_form_scopes_choices_once_the_organization_is_chosen(self, org_a, org_b, make_location, make_type):
        from apps.locations.admin import LocationAdminForm

        own = make_location(org_a, "Own")
        foreign = make_location(org_b, "Foreign")
        form = LocationAdminForm(data={"organization": str(org_a.pk)})
        assert own in form.fields["parent"].queryset and foreign not in form.fields["parent"].queryset
    def test_cycle_via_the_admin_is_rejected(self, admin_client, chain_a, org_a):
        root, child = chain_a[0], chain_a[1]
        data = {
            "location_type": str(root.location_type_id), "parent": str(child.pk), "name": root.name,
            "code": root.code, "is_active": "on", "metadata": "{}", "_save": "Save",
        }
        response = admin_client.post(change_url("location", root), data)
        assert response.status_code == 200
        root.refresh_from_db()
        assert root.parent_id is None

    def test_organization_is_read_only_once_created(self, admin_client, org_a, org_b, make_location):
        location = make_location(org_a, "L")
        assert 'name="organization"' not in admin_client.get(change_url("location", location)).content.decode()
        data = self.payload(org_b, location.location_type, name="L", code=location.code)
        admin_client.post(change_url("location", location), data)
        location.refresh_from_db()
        assert location.organization_id == org_a.pk

    def test_deactivating_with_active_children_is_refused_in_the_admin(self, admin_client, chain_a):
        root = chain_a[0]
        data = {"location_type": str(root.location_type_id), "name": root.name, "code": root.code,
                "metadata": "{}", "_save": "Save"}  # is_active omitted == False
        response = admin_client.post(change_url("location", root), data)
        assert response.status_code == 200 and "active child locations" in response.content.decode()
        root.refresh_from_db()
        assert root.is_active

    def test_toggling_is_active_logs_the_specific_event(self, admin_client, org_a, make_location):
        leaf = make_location(org_a, "Leaf")
        data = {"location_type": str(leaf.location_type_id), "name": "Leaf", "code": leaf.code, "metadata": "{}", "_save": "Save"}
        assert admin_client.post(change_url("location", leaf), data).status_code == 302
        assert ActivityLog.objects.filter(verb="location.deactivated", metadata__source="admin").exists()

    def test_changing_the_parent_logs_parent_changed(self, admin_client, org_a, make_location):
        a, b = make_location(org_a, "A"), make_location(org_a, "B")
        data = {"location_type": str(b.location_type_id), "parent": str(a.pk), "name": "B", "code": b.code,
                "is_active": "on", "metadata": "{}", "_save": "Save"}
        assert admin_client.post(change_url("location", b), data).status_code == 302
        assert ActivityLog.objects.filter(verb="location.parent_changed").exists()

    def test_locations_cannot_be_deleted_from_the_admin(self, admin_client, org_a, make_location):
        location = make_location(org_a, "L")
        url = reverse("admin:locations_location_delete", args=[location.pk])
        assert admin_client.get(url).status_code == 403
        assert Location.objects.filter(pk=location.pk).exists()


class TestLocationTypeAdmin:
    def test_create_a_platform_type(self, admin_client):
        response = admin_client.post(add_url("locationtype"), {"name": "Site", "code": "site", "sort_order": 0, "is_active": "on", "_save": "Save"})
        assert response.status_code == 302 and LocationType.objects.get(code="SITE").is_platform_type

    def test_duplicate_code_in_an_organization(self, admin_client, org_a, make_type):
        make_type(org_a, "One", "DUP")
        response = admin_client.post(
            add_url("locationtype"),
            {"organization": str(org_a.pk), "name": "Two", "code": "dup", "sort_order": 0, "is_active": "on", "_save": "Save"},
        )
        assert response.status_code == 200 and "already exists" in response.content.decode()

    def test_toggling_a_type_is_logged(self, admin_client, org_a, make_type):
        lt = make_type(org_a, "T", "T")
        data = {"organization": str(org_a.pk), "name": "T", "code": "T", "sort_order": 0, "_save": "Save"}
        admin_client.post(change_url("locationtype", lt), data)
        assert ActivityLog.objects.filter(verb="location_type.deactivated").exists()

    def test_a_type_in_use_cannot_be_deleted(self, admin_client, org_a, make_location):
        location = make_location(org_a, "L")
        url = reverse("admin:locations_locationtype_delete", args=[location.location_type_id])
        response = admin_client.post(url, {"post": "yes"})
        assert LocationType.objects.filter(pk=location.location_type_id).exists()
        assert response.status_code in (200, 302)

    def test_rehoming_a_type_used_elsewhere_is_rejected(self, admin_client, org_a, org_b, make_type, make_location):
        shared = make_type(None, "Shared", "SHARED")
        make_location(org_b, "B", type=shared)
        data = {"organization": str(org_a.pk), "name": "Shared", "code": "SHARED", "sort_order": 0, "is_active": "on", "_save": "Save"}
        response = admin_client.post(change_url("locationtype", shared), data)
        assert response.status_code == 200
        shared.refresh_from_db()
        assert shared.organization_id is None
