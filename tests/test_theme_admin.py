"""Theme administration in the Django/Jazzmin admin."""

from __future__ import annotations

import re

import pytest
from django.urls import reverse

from apps.core.models import ActivityLog
from apps.themes.constants import ThemeStatus
from apps.themes.models import Theme

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client(client, superuser):
    client.force_login(superuser)
    return client


def admin_url(name, *args):
    return reverse(f"admin:themes_{name}", args=args)


class TestPages:
    def test_changelist_add_and_change_render(self, admin_client, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        assert admin_client.get(admin_url("theme_changelist")).status_code == 200
        assert admin_client.get(admin_url("theme_add")).status_code == 200
        assert admin_client.get(admin_url("theme_change", theme.pk)).status_code == 200

    def test_history_and_ledger_pages_render(self, admin_client, org_a, make_theme):
        theme = make_theme(org_a, "Brand", status=ThemeStatus.ACTIVE)
        assert admin_client.get(admin_url("theme_history", theme.pk)).status_code == 200
        assert admin_client.get(admin_url("themeactivation_changelist")).status_code == 200
        assert admin_client.get(admin_url("userappearance_changelist")).status_code == 200

    def test_sidebar_lists_the_theme_models_with_icons(self, admin_client):
        html = admin_client.get("/admin/").content.decode()
        assert "Themes" in html and "fa-palette" in html

    def test_changelist_filters_and_search(self, admin_client, org_a, make_theme):
        make_theme(org_a, "Brand")
        for query in ("?status__exact=draft", "?scope__exact=organization", "?q=brand"):
            assert admin_client.get(admin_url("theme_changelist") + query).status_code == 200


class TestNoRawTokenEditing:
    def test_change_form_has_no_token_or_css_inputs(self, admin_client, org_a, make_theme):
        theme = make_theme(org_a, "Brand", tokens={"light": {"primary": "#112233"}})
        html = admin_client.get(admin_url("theme_change", theme.pk)).content.decode()
        assert not re.search(r'name="tokens"', html)
        assert "<textarea" not in html.replace('name="description"', "")
        assert "custom_css" not in html and "custom_js" not in html
        assert "Open the theme editor" in html and "#112233" in html  # read-only summary + link

    def test_published_theme_is_read_only(self, admin_client, org_a, make_theme):
        theme = make_theme(org_a, "Live", status=ThemeStatus.ACTIVE)
        html = admin_client.get(admin_url("theme_change", theme.pk)).content.decode()
        assert 'name="name"' not in html
        assert "View the theme" in html

    def test_delete_is_not_offered(self, admin_client, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        assert admin_client.get(reverse("admin:themes_theme_delete", args=[theme.pk])).status_code == 403


class TestCreateThroughServices:
    def test_add_creates_a_draft_from_a_preset(self, admin_client, org_a, superuser):
        response = admin_client.post(admin_url("theme_add"), {
            "organization": org_a.pk, "name": "Corporate", "code": "", "preset": "corporate",
            "description": "", "default_appearance": "system",
        })
        assert response.status_code == 302
        theme = Theme.objects.get()
        assert theme.status == ThemeStatus.DRAFT and theme.code == "CORPORATE"
        assert theme.tokens["light"]["primary"] == "#0b3d91"
        assert theme.created_by == superuser
        assert ActivityLog.objects.filter(verb="theme.created", actor=superuser).exists()

    def test_inactive_organizations_are_refused(self, admin_client, org_a):
        org_a.status = "inactive"
        org_a.save()
        response = admin_client.post(admin_url("theme_add"), {
            "organization": org_a.pk, "name": "X", "preset": "", "default_appearance": "system",
        })
        assert response.status_code == 200 and not Theme.objects.exists()

    def test_system_theme_without_organization(self, admin_client):
        admin_client.post(admin_url("theme_add"), {"organization": "", "name": "Platform", "preset": "", "default_appearance": "system"})
        assert Theme.objects.get().scope == "system"

    def test_editing_a_draft_goes_through_the_service(self, admin_client, org_a, make_theme, superuser):
        theme = make_theme(org_a, "Brand")
        admin_client.post(admin_url("theme_change", theme.pk), {"name": "Renamed", "description": "d", "default_appearance": "dark"})
        theme.refresh_from_db()
        assert (theme.name, theme.default_appearance, theme.code) == ("Renamed", "dark", "BRAND")
        assert ActivityLog.objects.filter(verb="theme.updated", actor=superuser).exists()


class TestActions:
    def act(self, client, action, *themes):
        return client.post(
            admin_url("theme_changelist"),
            {"action": action, "_selected_action": [str(t.pk) for t in themes]},
            follow=True,
        )

    def test_publish_activate_archive(self, admin_client, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        self.act(admin_client, "publish_selected", theme)
        assert Theme.objects.get(pk=theme.pk).status == ThemeStatus.PUBLISHED
        self.act(admin_client, "activate_selected", theme)
        assert Theme.objects.get(pk=theme.pk).status == ThemeStatus.ACTIVE
        other = make_theme(org_a, "Other")
        self.act(admin_client, "archive_selected", other)
        assert Theme.objects.get(pk=other.pk).status == ThemeStatus.ARCHIVED

    def test_rollback(self, admin_client, org_a, make_theme):
        from apps.themes import services

        first = make_theme(org_a, "First", status=ThemeStatus.ACTIVE)
        second = services.activate_theme(make_theme(org_a, "Second", status=ThemeStatus.PUBLISHED))
        self.act(admin_client, "rollback_selected", second)
        assert Theme.objects.get(pk=first.pk).status == ThemeStatus.ACTIVE

    def test_failures_are_reported_not_raised(self, admin_client, org_a, make_theme):
        pale = make_theme(org_a, "Pale", tokens={"light": {"foreground": "#dddddd"}})
        response = self.act(admin_client, "publish_selected", pale)
        assert "Text on page background" in response.content.decode()
        assert Theme.objects.get(pk=pale.pk).status == ThemeStatus.DRAFT


class TestPermissions:
    def test_staff_without_theme_permissions_see_nothing(self, client, make_user):
        staff = make_user(staff=True)
        client.force_login(staff)
        assert client.get(admin_url("theme_changelist")).status_code == 403

    def test_view_only_staff_cannot_change_or_add(self, client, make_user, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        staff = make_user(staff=True, perms=("themes.view_theme",))
        client.force_login(staff)
        assert client.get(admin_url("theme_changelist")).status_code == 200
        assert client.get(admin_url("theme_add")).status_code == 403
        client.post(admin_url("theme_change", theme.pk), {"name": "Hacked", "default_appearance": "system"})
        theme.refresh_from_db()
        assert theme.name == "Brand"
