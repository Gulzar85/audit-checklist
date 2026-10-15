"""The theme engine's web layer: stylesheet, appearance switch, management UI, editor and preview."""

from __future__ import annotations

import json
import re
from html import unescape

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.core.models import ActivityLog
from apps.themes import services
from apps.themes.constants import APPEARANCE_COOKIE, PREVIEW_SESSION_KEY, ThemeStatus
from apps.themes.models import Theme, UserAppearance

pytestmark = pytest.mark.django_db


def url(name, *args):
    return reverse(f"themes:{name}", args=args)


def vars_in(response):
    """The ``data-preview-vars`` dictionary of a preview fragment."""
    match = re.search(r'data-preview-vars="([^"]*)"', response.content.decode())
    return json.loads(unescape(match.group(1)))


class TestStylesheet:
    def test_is_public_and_css(self, client, db):
        response = client.get(url("tokens"))
        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/css")
        body = response.content.decode()
        assert ':root, [data-theme="light"]' in body and '[data-theme="dark"]' in body

    def test_is_private_and_revalidated(self, client, db):
        response = client.get(url("tokens"))
        assert response["Cache-Control"] == "private, no-cache"
        assert "Cookie" in response["Vary"]

    def test_conditional_request_returns_304(self, client, db):
        first = client.get(url("tokens"))
        second = client.get(url("tokens"), HTTP_IF_NONE_MATCH=first["ETag"])
        assert second.status_code == 304 and not second.content

    def test_etag_changes_with_the_theme(self, client, make_theme):
        before = client.get(url("tokens"))["ETag"]
        make_theme(None, "System", status=ThemeStatus.ACTIVE, tokens={"light": {"primary": "#112233"}})
        after = client.get(url("tokens"))
        assert after["ETag"] != before and "#112233" in after.content.decode()

    def test_each_organization_gets_its_own_tokens(self, client, make_theme, make_user, org_a, org_b):
        make_theme(org_a, "A", status=ThemeStatus.ACTIVE, tokens={"light": {"primary": "#aa0000"}})
        make_theme(org_b, "B", status=ThemeStatus.ACTIVE, tokens={"light": {"primary": "#0000aa"}})
        client.force_login(make_user(organization=org_a))
        body_a = client.get(url("tokens")).content.decode()
        client.force_login(make_user(organization=org_b))
        body_b = client.get(url("tokens")).content.decode()
        assert "#aa0000" in body_a and "#0000aa" not in body_a
        assert "#0000aa" in body_b and "#aa0000" not in body_b

    def test_anonymous_visitors_never_see_organization_themes(self, client, make_theme, org_a):
        make_theme(org_a, "A", status=ThemeStatus.ACTIVE, tokens={"light": {"primary": "#aa0000"}})
        assert "#aa0000" not in client.get(url("tokens")).content.decode()

    def test_post_is_not_allowed(self, client, db):
        assert client.post(url("tokens")).status_code == 405


class TestPageShell:
    def test_html_carries_the_mode_and_links_the_stylesheet(self, client, db):
        html = client.get("/").content.decode()
        assert '<html lang="en" class="h-full" data-theme="system">' in html
        assert 'href="/themes/tokens.css?v=' in html
        assert "<style" not in html  # the theme never arrives as an inline style

    def test_signed_in_users_saved_mode_is_rendered_on_first_byte(self, client, user):
        services.set_user_appearance(user, "dark")
        client.force_login(user)
        assert 'data-theme="dark"' in client.get("/").content.decode()

    def test_anonymous_cookie_is_rendered_on_first_byte(self, client, db):
        client.cookies[APPEARANCE_COOKIE] = "dark"
        assert 'data-theme="dark"' in client.get("/").content.decode()

    def test_theme_color_metas_follow_the_mode(self, client, user):
        client.force_login(user)
        system = client.get("/").content.decode()
        assert 'media="(prefers-color-scheme: light)"' in system and 'media="(prefers-color-scheme: dark)"' in system
        services.set_user_appearance(user, "dark")
        dark = client.get("/").content.decode()
        assert re.search(r'data-scheme="dark" media="all"', dark) and re.search(r'data-scheme="light" media="not all"', dark)

    def test_page_has_no_inline_script_or_event_handlers_for_theming(self, client, user):
        client.force_login(user)
        html = client.get("/").content.decode()
        assert not re.search(r"<script(?![^>]*\bsrc=)", html)
        assert not re.search(r"\son(click|load|change)=", html)

    def test_appearance_menu_offers_three_modes(self, client, user):
        client.force_login(user)
        html = client.get("/").content.decode()
        for label in ("Light", "Dark", "System"):
            assert f"{label}\n" in html or f"> {label}" in html
        assert 'data-persist-url="/themes/appearance/"' in html

    def test_anonymous_menu_does_not_persist_to_the_server(self, client, db):
        assert "data-persist-url" not in client.get("/").content.decode()

    def test_csp_is_not_weakened(self, client, db):
        header = client.get("/")["Content-Security-Policy"]
        assert "'unsafe-inline'" not in header and "'unsafe-eval'" not in header
        assert "style-src 'self'" in header or "style-src 'self' 'nonce-" in header

    def test_error_pages_render_without_a_theme_stylesheet_link_failure(self, client, db):
        response = client.get("/definitely-missing/")
        assert response.status_code == 404


class TestAppearanceEndpoint:
    def test_anonymous_sets_a_cookie_only(self, client, db):
        response = client.post(url("appearance"), {"mode": "dark"})
        assert response.status_code == 204
        assert response.cookies[APPEARANCE_COOKIE].value == "dark"
        assert not UserAppearance.objects.exists()

    def test_signed_in_users_choice_is_stored(self, client, user):
        client.force_login(user)
        assert client.post(url("appearance"), {"mode": "light"}).status_code == 204
        assert UserAppearance.objects.get(user=user).mode == "light"
        assert ActivityLog.objects.filter(verb="appearance.preference_changed", actor=user).exists()

    def test_invalid_modes_are_rejected(self, client, user):
        client.force_login(user)
        assert client.post(url("appearance"), {"mode": "<script>"}).status_code == 400
        assert not UserAppearance.objects.exists()

    def test_empty_mode_clears_the_preference(self, client, user):
        services.set_user_appearance(user, "dark")
        client.force_login(user)
        response = client.post(url("appearance"), {"mode": ""})
        assert response.status_code == 204 and not UserAppearance.objects.exists()

    def test_get_is_not_allowed(self, client, user):
        client.force_login(user)
        assert client.get(url("appearance")).status_code == 405

    def test_one_users_choice_does_not_touch_another(self, client, make_user, org_a):
        one, two = make_user(organization=org_a), make_user(organization=org_a)
        client.force_login(one)
        client.post(url("appearance"), {"mode": "dark"})
        assert not UserAppearance.objects.filter(user=two).exists()


class TestListAndDetail:
    def test_requires_login(self, client, db):
        response = client.get(url("list"))
        assert response.status_code == 302 and "/auth/" in response["Location"]

    def test_members_see_their_organizations_published_themes_only(self, client, viewer_a, org_a, org_b, make_theme):
        make_theme(org_a, "Visible A", status=ThemeStatus.PUBLISHED)
        make_theme(org_a, "Secret draft")
        make_theme(org_b, "Foreign B", status=ThemeStatus.PUBLISHED)
        client.force_login(viewer_a)
        html = client.get(url("list")).content.decode()
        assert "Visible A" in html
        assert "Secret draft" not in html and "Foreign B" not in html

    def test_designers_see_drafts_of_their_own_organization(self, client, theme_admin_a, org_a, make_theme):
        make_theme(org_a, "My draft")
        client.force_login(theme_admin_a)
        assert "My draft" in client.get(url("list")).content.decode()

    def test_organization_filter_lists_only_visible_organizations(self, client, theme_admin_a, org_a, org_b):
        client.force_login(theme_admin_a)
        html = client.get(url("list")).content.decode()
        assert "Org A" in html and "Org B" not in html

    def test_detail_of_a_foreign_theme_is_404(self, client, theme_admin_a, org_b, make_theme):
        theme = make_theme(org_b, "Foreign", status=ThemeStatus.PUBLISHED)
        client.force_login(theme_admin_a)
        assert client.get(url("detail", theme.pk)).status_code == 404

    def test_detail_shows_palette_accessibility_and_history(self, client, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand", tokens={"light": {"primary": "#112233"}}, actor=theme_admin_a)
        client.force_login(theme_admin_a)
        html = client.get(url("detail", theme.pk)).content.decode()
        assert "#112233" in html and "WCAG 2.2 AA" in html and "Change history" in html

    def test_viewer_sees_no_management_buttons(self, client, viewer_a, org_a, make_theme):
        theme = make_theme(org_a, "Pub", status=ThemeStatus.PUBLISHED)
        client.force_login(viewer_a)
        html = client.get(url("detail", theme.pk)).content.decode()
        for action in ("activate", "publish", "archive", "new-version", "/edit/"):
            assert f"/{theme.pk}/{action.strip('/')}" not in html

    def test_buttons_follow_the_lifecycle(self, client, theme_admin_a, org_a, make_theme):
        draft = make_theme(org_a, "Draft")
        client.force_login(theme_admin_a)
        html = client.get(url("detail", draft.pk)).content.decode()
        assert url("publish", draft.pk) in html and url("edit", draft.pk) in html
        assert url("activate", draft.pk) not in html

    def test_link_in_navigation(self, client, viewer_a):
        client.force_login(viewer_a)
        assert url("list") in client.get("/").content.decode()


class TestCreate:
    def test_requires_permission(self, client, viewer_a):
        client.force_login(viewer_a)
        assert client.get(url("create")).status_code == 403

    def test_designer_creates_a_draft_and_lands_in_the_editor(self, client, theme_admin_a, org_a):
        client.force_login(theme_admin_a)
        response = client.post(url("create"), {
            "organization": org_a.pk, "name": "Brand", "description": "", "preset": "corporate", "default_appearance": "system",
        })
        theme = Theme.objects.get(name="Brand")
        assert response.status_code == 302 and response["Location"] == url("edit", theme.pk)
        assert theme.status == ThemeStatus.DRAFT and theme.tokens["light"]["primary"] == "#0b3d91"

    def test_cannot_create_in_another_organization(self, client, theme_admin_a, org_b):
        client.force_login(theme_admin_a)
        response = client.post(url("create"), {"organization": org_b.pk, "name": "Evil", "default_appearance": "system"})
        assert response.status_code == 200 and not Theme.objects.exists()

    def test_organization_members_cannot_create_system_themes(self, client, theme_admin_a):
        client.force_login(theme_admin_a)
        response = client.post(url("create"), {"organization": "", "name": "Sys", "default_appearance": "system"})
        assert response.status_code == 200 and not Theme.objects.exists()

    def test_staff_can_create_system_themes(self, client, theme_staff):
        client.force_login(theme_staff)
        response = client.post(url("create"), {"organization": "", "name": "Platform", "default_appearance": "system"})
        assert response.status_code == 302
        assert Theme.objects.get().organization is None


class TestEditor:
    def test_editor_exposes_controls_not_css(self, client, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        client.force_login(theme_admin_a)
        html = client.get(url("edit", theme.pk)).content.decode()
        for section in ("General", "Colors", "Typography", "Shape", "Density", "Navigation", "Components", "Light mode", "Dark mode"):
            assert f">{section}<" in html
        assert 'type="color"' in html and "Live preview" in html
        assert "<textarea" not in html.replace('name="description"', "") or "custom_css" not in html
        assert "custom_css" not in html and "custom_js" not in html

    def test_mode_radios_outside_the_form_post_the_form_themselves(self, client, theme_admin_a, org_a, make_theme):
        """Regression: radios with ``form=`` live outside <form>, so their change events never reach it."""
        theme = make_theme(org_a, "Brand")
        client.force_login(theme_admin_a)
        html = client.get(url("edit", theme.pk)).content.decode()
        assert re.search(r'<fieldset[^>]*hx-post="[^"]*/preview/"[^>]*hx-include="#theme-form"[^>]*hx-trigger="change"', html)

    def test_editor_renders_the_real_components(self, client, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        client.force_login(theme_admin_a)
        html = client.get(url("edit", theme.pk)).content.decode()
        for cls in ("btn btn-primary", "alert alert-danger", "badge badge-success", "table", "card", "modal-dialog", "nav-link"):
            assert cls in html

    def test_save_stores_only_values_that_differ_from_the_base(self, client, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        client.force_login(theme_admin_a)
        form = client.get(url("edit", theme.pk)).context["form"]
        data = {name: field.initial for name, field in form.fields.items() if field.initial is not None}
        data.update({"name": "Brand", "default_appearance": "dark", "light__primary": "#112233", "shared__radius_md": "10"})
        response = client.post(url("edit", theme.pk), data)
        assert response.status_code == 302
        theme.refresh_from_db()
        assert theme.tokens == {"light": {"primary": "#112233"}, "shared": {"radius_md": 10}}
        assert theme.default_appearance == "dark"

    def test_invalid_values_are_rejected_and_nothing_is_saved(self, client, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        client.force_login(theme_admin_a)
        form = client.get(url("edit", theme.pk)).context["form"]
        data = {name: field.initial for name, field in form.fields.items() if field.initial is not None}
        data.update({"name": "Brand", "default_appearance": "system",
                     "light__primary": "red; background:url(x)", "shared__font_size_base": "999"})
        response = client.post(url("edit", theme.pk), data)
        assert response.status_code == 200
        theme.refresh_from_db()
        assert theme.tokens == {}

    def test_published_themes_cannot_be_edited(self, client, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Live", status=ThemeStatus.ACTIVE)
        client.force_login(theme_admin_a)
        response = client.get(url("edit", theme.pk))
        assert response.status_code == 302 and response["Location"] == url("detail", theme.pk)
        assert client.post(url("edit", theme.pk), {"name": "Hacked"}).status_code == 302
        theme.refresh_from_db()
        assert theme.name == "Live"

    def test_viewers_cannot_edit(self, client, viewer_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        client.force_login(viewer_a)
        assert client.get(url("edit", theme.pk)).status_code == 404  # drafts are invisible to viewers

    def test_other_organizations_cannot_edit(self, client, theme_admin_b, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        client.force_login(theme_admin_b)
        assert client.get(url("edit", theme.pk)).status_code == 404
        assert client.post(url("preview", theme.pk), {}).status_code == 404


class TestLivePreview:
    def _post(self, client, theme, **overrides):
        form = client.get(url("edit", theme.pk)).context["form"]
        data = {name: field.initial for name, field in form.fields.items() if field.initial is not None}
        data.update({"name": "Brand", "default_appearance": "system", **overrides})
        return client.post(url("preview", theme.pk), data)

    def test_returns_css_variables_for_the_unsaved_values(self, client, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        client.force_login(theme_admin_a)
        response = self._post(client, theme, light__primary="#ff00aa", shared__radius_md="14")
        variables = vars_in(response)
        assert variables["--color-primary"] == "#ff00aa" and variables["--radius-md"] == "0.875rem"
        theme.refresh_from_db()
        assert theme.tokens == {}  # nothing was saved

    def test_dark_mode_preview_returns_dark_variables(self, client, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        client.force_login(theme_admin_a)
        variables = vars_in(self._post(client, theme, preview_mode="dark", dark__primary="#abcdef"))
        assert variables["--color-primary"] == "#abcdef"

    def test_reports_contrast_problems_live(self, client, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        client.force_login(theme_admin_a)
        html = self._post(client, theme, light__foreground="#dddddd").content.decode()
        assert "cannot be published" in html and "Text on page background" in html

    def test_invalid_values_are_reported_not_applied(self, client, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        client.force_login(theme_admin_a)
        response = self._post(client, theme, light__primary="not-a-colour")
        assert "Some values are not valid yet" in response.content.decode()
        assert vars_in(response)["--color-primary"] == "#4f46e5"

    def test_variables_only_contain_custom_properties(self, client, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        client.force_login(theme_admin_a)
        for name, value in vars_in(self._post(client, theme)).items():
            assert re.fullmatch(r"--[a-z0-9-]+", name)
            assert not re.search(r"[<>{};]|url\(|@import|javascript:", value)

    def test_get_is_not_allowed(self, client, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        client.force_login(theme_admin_a)
        assert client.get(url("preview", theme.pk)).status_code == 405


class TestActions:
    def post(self, client, name, theme, **data):
        return client.post(url(name, theme.pk), data)

    def test_publish_then_activate(self, client, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        client.force_login(theme_admin_a)
        assert self.post(client, "publish", theme).status_code == 302
        assert Theme.objects.get(pk=theme.pk).status == ThemeStatus.PUBLISHED
        self.post(client, "activate", theme)
        assert Theme.objects.get(pk=theme.pk).status == ThemeStatus.ACTIVE

    def test_failing_publish_shows_the_reason(self, client, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Pale", tokens={"light": {"foreground": "#dddddd"}})
        client.force_login(theme_admin_a)
        response = client.post(url("publish", theme.pk), follow=True)
        assert "Text on page background" in response.content.decode()
        assert Theme.objects.get(pk=theme.pk).status == ThemeStatus.DRAFT

    def test_actions_require_post(self, client, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        client.force_login(theme_admin_a)
        for name in ("publish", "activate", "rollback", "archive", "new_version", "try"):
            assert client.get(url(name, theme.pk)).status_code == 405

    def test_designer_without_activate_permission_gets_403(self, client, make_user, org_a, make_theme):
        designer = make_user(organization=org_a, perms=("themes.view_theme", "themes.change_theme", "themes.publish_theme"))
        theme = make_theme(org_a, "Brand", status=ThemeStatus.PUBLISHED)
        client.force_login(designer)
        assert self.post(client, "activate", theme).status_code == 403
        assert Theme.objects.get(pk=theme.pk).status == ThemeStatus.PUBLISHED

    def test_members_cannot_activate(self, client, viewer_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand", status=ThemeStatus.PUBLISHED)
        client.force_login(viewer_a)
        assert self.post(client, "activate", theme).status_code == 403

    def test_other_organizations_get_404(self, client, theme_admin_b, org_a, make_theme):
        theme = make_theme(org_a, "Brand", status=ThemeStatus.PUBLISHED)
        client.force_login(theme_admin_b)
        for name in ("publish", "activate", "archive", "new_version", "rollback", "try"):
            assert self.post(client, name, theme).status_code == 404
        assert Theme.objects.get(pk=theme.pk).status == ThemeStatus.PUBLISHED

    def test_org_admins_cannot_activate_system_themes(self, client, theme_admin_a, make_theme):
        system = make_theme(None, "Platform", status=ThemeStatus.PUBLISHED)
        client.force_login(theme_admin_a)
        assert self.post(client, "activate", system).status_code == 403

    def test_staff_can_activate_system_themes(self, client, theme_staff, make_theme):
        system = make_theme(None, "Platform", status=ThemeStatus.PUBLISHED)
        client.force_login(theme_staff)
        self.post(client, "activate", system)
        assert Theme.objects.get(pk=system.pk).status == ThemeStatus.ACTIVE

    def test_rollback_flow(self, client, theme_admin_a, org_a, make_theme):
        first = make_theme(org_a, "First", status=ThemeStatus.ACTIVE)
        second = services.activate_theme(make_theme(org_a, "Second", status=ThemeStatus.PUBLISHED))
        client.force_login(theme_admin_a)
        self.post(client, "rollback", second)
        assert Theme.objects.get(pk=first.pk).status == ThemeStatus.ACTIVE

    def test_new_version_and_archive(self, client, theme_admin_a, org_a, make_theme):
        live = make_theme(org_a, "Live", status=ThemeStatus.ACTIVE)
        client.force_login(theme_admin_a)
        self.post(client, "new_version", live)
        assert Theme.objects.filter(code=live.code, version=2, status="draft").exists()
        published = make_theme(org_a, "Old", status=ThemeStatus.PUBLISHED)
        self.post(client, "archive", published)
        assert Theme.objects.get(pk=published.pk).status == ThemeStatus.ARCHIVED

    def test_clone(self, client, theme_admin_a, org_a, make_theme):
        original = make_theme(org_a, "Corporate", status=ThemeStatus.ACTIVE)
        client.force_login(theme_admin_a)
        assert client.get(url("clone", original.pk)).status_code == 200
        response = client.post(url("clone", original.pk), {"name": "Corporate Dark", "organization": org_a.pk})
        clone = Theme.objects.get(name="Corporate Dark")
        assert response.status_code == 302 and clone.status == ThemeStatus.DRAFT and clone.code != original.code

    def test_clone_into_a_foreign_organization_is_refused(self, client, theme_admin_a, org_a, org_b, make_theme):
        original = make_theme(org_a, "Corporate", status=ThemeStatus.PUBLISHED)
        client.force_login(theme_admin_a)
        client.post(url("clone", original.pk), {"name": "Leak", "organization": org_b.pk})
        assert not Theme.objects.filter(name="Leak").exists()


class TestTemporaryPreview:
    def test_try_then_stop(self, client, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Candidate", tokens={"light": {"primary": "#123456"}})
        client.force_login(theme_admin_a)
        client.post(url("try", theme.pk))
        assert client.session[PREVIEW_SESSION_KEY] == str(theme.pk)
        html = client.get("/").content.decode()
        assert "Previewing theme" in html
        assert "#123456" in client.get(url("tokens")).content.decode()
        client.post(url("preview_stop"))
        assert PREVIEW_SESSION_KEY not in client.session
        assert "Previewing theme" not in client.get("/").content.decode()
        assert "#123456" not in client.get(url("tokens")).content.decode()

    def test_preview_affects_only_the_previewing_session(self, client, make_user, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Candidate", tokens={"light": {"primary": "#123456"}})
        client.force_login(theme_admin_a)
        client.post(url("try", theme.pk))
        colleague = make_user(organization=org_a)
        client.logout()
        client.force_login(colleague)
        assert "#123456" not in client.get(url("tokens")).content.decode()

    def test_stop_ignores_open_redirects(self, client, user):
        client.force_login(user)
        response = client.post(url("preview_stop"), {"next": "https://evil.example/"})
        assert response["Location"] == url("list")


class TestExportImportViews:
    def test_export_downloads_json(self, client, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand", tokens={"light": {"primary": "#112233"}})
        client.force_login(theme_admin_a)
        response = client.get(url("export", theme.pk))
        assert response.status_code == 200 and "attachment" in response["Content-Disposition"]
        assert response.json()["tokens"] == {"light": {"primary": "#112233"}}

    def test_viewers_cannot_export_drafts(self, client, viewer_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        client.force_login(viewer_a)
        assert client.get(url("export", theme.pk)).status_code == 404

    def test_import_creates_a_draft(self, client, theme_admin_a, org_a, org_b, make_theme):
        document = services.export_theme(make_theme(org_b, "Brand", tokens={"light": {"primary": "#112233"}}))
        upload = SimpleUploadedFile("t.json", json.dumps(document).encode(), content_type="application/json")
        client.force_login(theme_admin_a)
        response = client.post(url("import"), {"organization": org_a.pk, "file": upload})
        assert response.status_code == 302
        imported = Theme.objects.get(organization=org_a)
        assert imported.status == ThemeStatus.DRAFT and imported.tokens == {"light": {"primary": "#112233"}}

    def test_hostile_import_is_rejected(self, client, theme_admin_a, org_a):
        document = {"format": "checklist-platform.theme/1", "name": "x", "tokens": {"light": {"primary": "url(//evil)"}}}
        upload = SimpleUploadedFile("t.json", json.dumps(document).encode())
        client.force_login(theme_admin_a)
        response = client.post(url("import"), {"organization": org_a.pk, "file": upload})
        assert response.status_code == 200 and not Theme.objects.exists()

    def test_import_needs_permission(self, client, viewer_a):
        client.force_login(viewer_a)
        assert client.get(url("import")).status_code == 403


class TestGalleryAndPwa:
    def test_gallery_renders_the_real_components(self, client, user):
        client.force_login(user)
        html = client.get(url("gallery")).content.decode()
        for cls in ("btn-primary", "btn-danger", "alert-warning", "badge-info", "pagination", "modal-dialog"):
            assert cls in html

    def test_manifest_uses_the_organizations_theme_colors(self, client, make_theme, make_user, org_a):
        make_theme(org_a, "Brand", status=ThemeStatus.ACTIVE,
                   tokens={"light": {"sidebar_background": "#0b2447", "sidebar_foreground": "#ffffff",
                                     "background": "#eeeeee"}})
        client.force_login(make_user(organization=org_a))
        manifest = client.get("/manifest.webmanifest").json()
        assert manifest["theme_color"] == "#0b2447" and manifest["background_color"] == "#eeeeee"

    def test_manifest_defaults_without_a_theme(self, client, db):
        manifest = client.get("/manifest.webmanifest").json()
        assert re.fullmatch(r"#[0-9a-f]{6}", manifest["theme_color"])

    def test_manifest_link_sends_credentials(self, client, db):
        assert 'rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials"' in client.get("/").content.decode()

    def test_service_worker_handles_the_theme_stylesheet_network_first(self, client, db):
        source = client.get("/service-worker.js").content.decode()
        assert "/themes/tokens.css" in source and "THEME_STYLESHEET" in source
