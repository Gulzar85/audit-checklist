"""The theme API: listing, lifecycle, preview, permissions, tenant isolation and appearance."""

from __future__ import annotations

import pytest

from apps.themes import services
from apps.themes.constants import ThemeStatus
from apps.themes.models import Theme, UserAppearance

pytestmark = pytest.mark.django_db

LIST = "/api/v1/themes/"


def data(response):
    """Unwrap the standard envelope (``{"data": ...}``) if present."""
    body = response.json()
    return body.get("data", body) if isinstance(body, dict) else body


def results(response):
    payload = data(response)
    return payload["results"] if isinstance(payload, dict) and "results" in payload else payload


@pytest.fixture
def api(client):
    def _login(user):
        client.force_login(user)
        return client

    return _login


class TestAuthentication:
    def test_anonymous_is_rejected(self, client, db):
        assert client.get(LIST).status_code in (401, 403)
        assert client.get(LIST + "active/").status_code in (401, 403)
        assert client.get("/api/v1/preferences/appearance/").status_code in (401, 403)

    def test_inactive_users_are_rejected(self, client, make_user, org_a):
        user = make_user(organization=org_a, active=False)
        client.force_login(user)  # force_login of an inactive user is not authenticated
        assert client.get(LIST).status_code in (401, 403)


class TestListAndRetrieve:
    def test_lists_only_visible_themes(self, api, viewer_a, org_a, org_b, make_theme):
        make_theme(org_a, "Mine", status=ThemeStatus.PUBLISHED)
        make_theme(org_a, "Draft")
        make_theme(org_b, "Foreign", status=ThemeStatus.PUBLISHED)
        make_theme(None, "Platform", status=ThemeStatus.PUBLISHED)
        names = {t["name"] for t in results(api(viewer_a).get(LIST))}
        assert names == {"Mine", "Platform"}

    def test_designers_also_see_their_own_drafts_but_not_system_drafts(self, api, theme_admin_a, org_a, make_theme):
        make_theme(org_a, "Draft")
        make_theme(None, "System draft")
        names = {t["name"] for t in results(api(theme_admin_a).get(LIST))}
        assert names == {"Draft"}

    def test_retrieve_foreign_theme_is_404(self, api, theme_admin_a, org_b, make_theme):
        theme = make_theme(org_b, "Foreign", status=ThemeStatus.PUBLISHED)
        assert api(theme_admin_a).get(f"{LIST}{theme.pk}/").status_code == 404

    def test_unknown_id_looks_the_same_as_a_foreign_one(self, api, theme_admin_a, org_b, make_theme):
        theme = make_theme(org_b, "Foreign", status=ThemeStatus.PUBLISHED)
        client = api(theme_admin_a)
        foreign = client.get(f"{LIST}{theme.pk}/")
        missing = client.get(f"{LIST}00000000-0000-4000-8000-000000000000/")
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json()

    def test_representation(self, api, viewer_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand", status=ThemeStatus.ACTIVE, tokens={"light": {"primary": "#112233"}})
        payload = data(api(viewer_a).get(f"{LIST}{theme.pk}/"))
        assert payload["status"] == "active" and payload["is_active"] is True
        assert payload["tokens"] == {"light": {"primary": "#112233"}}
        assert "created_by" not in payload and "password" not in str(payload)

    def test_filtering(self, api, theme_admin_a, org_a, make_theme):
        make_theme(org_a, "Alpha", status=ThemeStatus.PUBLISHED)
        make_theme(org_a, "Beta")
        client = api(theme_admin_a)
        assert {t["name"] for t in results(client.get(LIST, {"status": "draft"}))} == {"Beta"}
        assert {t["name"] for t in results(client.get(LIST, {"q": "alp"}))} == {"Alpha"}
        assert {t["name"] for t in results(client.get(LIST, {"scope": "organization"}))} == {"Alpha", "Beta"}
        assert results(client.get(LIST, {"scope": "system"})) == []

    def test_cannot_filter_by_a_foreign_organization(self, api, theme_admin_a, org_b, make_theme):
        make_theme(org_b, "Foreign", status=ThemeStatus.PUBLISHED)
        response = api(theme_admin_a).get(LIST, {"organization": str(org_b.pk)})
        assert response.status_code == 400

    def test_search_and_ordering(self, api, theme_admin_a, org_a, make_theme):
        make_theme(org_a, "Zulu")
        make_theme(org_a, "Alpha")
        client = api(theme_admin_a)
        assert [t["name"] for t in results(client.get(LIST, {"ordering": "name"}))] == ["Alpha", "Zulu"]
        assert [t["name"] for t in results(client.get(LIST, {"search": "zul"}))] == ["Zulu"]


class TestCreateAndUpdate:
    def test_create(self, api, theme_admin_a, org_a):
        response = api(theme_admin_a).post(LIST, {"organization": str(org_a.pk), "name": "Brand", "preset": "corporate"})
        assert response.status_code == 201
        payload = data(response)
        assert payload["status"] == "draft" and payload["tokens"]["light"]["primary"] == "#0b3d91"

    def test_create_in_a_foreign_organization_is_rejected_as_unknown(self, api, theme_admin_a, org_b):
        response = api(theme_admin_a).post(LIST, {"organization": str(org_b.pk), "name": "Evil"})
        assert response.status_code == 400 and not Theme.objects.exists()

    def test_members_cannot_create(self, api, viewer_a, org_a):
        assert api(viewer_a).post(LIST, {"organization": str(org_a.pk), "name": "x"}).status_code == 403

    def test_org_admins_cannot_create_system_themes(self, api, theme_admin_a):
        assert api(theme_admin_a).post(LIST, {"name": "System"}).status_code == 403

    def test_staff_can_create_system_themes(self, api, theme_staff):
        response = api(theme_staff).post(LIST, {"name": "System"})
        assert response.status_code == 201 and data(response)["scope"] == "system"

    def test_invalid_tokens_are_rejected(self, api, theme_admin_a, org_a):
        response = api(theme_admin_a).post(
            LIST, {"organization": str(org_a.pk), "name": "x", "tokens": {"light": {"primary": "red"}}}, content_type="application/json"
        )
        assert response.status_code == 400 and not Theme.objects.exists()

    def test_patch_a_draft(self, api, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        response = api(theme_admin_a).patch(
            f"{LIST}{theme.pk}/", {"tokens": {"light": {"primary": "#112233"}}}, content_type="application/json"
        )
        assert response.status_code == 200
        theme.refresh_from_db()
        assert theme.tokens == {"light": {"primary": "#112233"}}

    def test_put_replaces_tokens(self, api, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand", tokens={"light": {"primary": "#112233"}})
        api(theme_admin_a).put(f"{LIST}{theme.pk}/", {"name": "Brand", "tokens": {}}, content_type="application/json")
        theme.refresh_from_db()
        assert theme.tokens == {}

    def test_published_themes_are_immutable(self, api, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Live", status=ThemeStatus.ACTIVE)
        response = api(theme_admin_a).patch(f"{LIST}{theme.pk}/", {"name": "Hacked"}, content_type="application/json")
        assert response.status_code == 400
        theme.refresh_from_db()
        assert theme.name == "Live"

    def test_code_cannot_be_changed(self, api, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        response = api(theme_admin_a).patch(f"{LIST}{theme.pk}/", {"code": "OTHER"}, content_type="application/json")
        assert response.status_code == 400

    def test_members_cannot_update(self, api, make_user, org_a, make_theme):
        theme = make_theme(org_a, "Pub", status=ThemeStatus.PUBLISHED)
        member = make_user(organization=org_a)
        assert api(member).patch(f"{LIST}{theme.pk}/", {"name": "x"}, content_type="application/json").status_code == 403

    def test_other_organizations_cannot_update(self, api, theme_admin_b, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        assert api(theme_admin_b).patch(f"{LIST}{theme.pk}/", {"name": "x"}, content_type="application/json").status_code == 404

    def test_no_delete(self, api, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        assert api(theme_admin_a).delete(f"{LIST}{theme.pk}/").status_code == 405


class TestLifecycleEndpoints:
    def test_publish_activate_archive(self, api, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        client = api(theme_admin_a)
        assert data(client.post(f"{LIST}{theme.pk}/publish/"))["status"] == "published"
        assert data(client.post(f"{LIST}{theme.pk}/activate/"))["status"] == "active"
        other = make_theme(org_a, "Other")
        assert data(client.post(f"{LIST}{other.pk}/archive/"))["status"] == "archived"

    def test_publish_blocked_by_contrast(self, api, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Pale", tokens={"light": {"foreground": "#dddddd"}})
        response = api(theme_admin_a).post(f"{LIST}{theme.pk}/publish/")
        assert response.status_code == 400 and "Text on page background" in str(response.json())

    def test_activating_a_draft_is_a_400(self, api, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        assert api(theme_admin_a).post(f"{LIST}{theme.pk}/activate/").status_code == 403  # not activatable yet

    def test_designer_without_activation_rights(self, api, make_user, org_a, make_theme):
        designer = make_user(organization=org_a, perms=("themes.view_theme", "themes.change_theme", "themes.publish_theme"))
        theme = make_theme(org_a, "Brand", status=ThemeStatus.PUBLISHED)
        client = api(designer)
        assert client.post(f"{LIST}{theme.pk}/activate/").status_code == 403
        assert client.post(f"{LIST}{theme.pk}/rollback/").status_code == 403

    def test_members_cannot_run_any_lifecycle_action(self, api, viewer_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand", status=ThemeStatus.PUBLISHED)
        client = api(viewer_a)
        for action in ("publish", "activate", "rollback", "archive", "new-version"):
            assert client.post(f"{LIST}{theme.pk}/{action}/").status_code == 403, action

    def test_cannot_touch_other_organizations(self, api, theme_admin_b, org_a, make_theme):
        theme = make_theme(org_a, "Brand", status=ThemeStatus.PUBLISHED)
        client = api(theme_admin_b)
        for action in ("publish", "activate", "rollback", "archive", "new-version", "clone", "preview", "export"):
            method = client.get if action in ("preview", "export") else client.post
            assert method(f"{LIST}{theme.pk}/{action}/").status_code == 404, action

    def test_system_themes_cannot_be_published_or_activated_by_org_admins(self, api, theme_admin_a, make_theme):
        system = make_theme(None, "Platform", status=ThemeStatus.PUBLISHED)
        client = api(theme_admin_a)
        assert client.post(f"{LIST}{system.pk}/activate/").status_code == 403
        assert client.post(f"{LIST}{system.pk}/archive/").status_code == 403
        assert client.patch(f"{LIST}{system.pk}/", {"name": "x"}, content_type="application/json").status_code == 403

    def test_rollback(self, api, theme_admin_a, org_a, make_theme):
        first = make_theme(org_a, "First", status=ThemeStatus.ACTIVE)
        second = services.activate_theme(make_theme(org_a, "Second", status=ThemeStatus.PUBLISHED))
        response = api(theme_admin_a).post(f"{LIST}{second.pk}/rollback/")
        assert response.status_code == 200 and data(response)["id"] == str(first.pk)

    def test_new_version(self, api, theme_admin_a, org_a, make_theme):
        live = make_theme(org_a, "Live", status=ThemeStatus.ACTIVE)
        response = api(theme_admin_a).post(f"{LIST}{live.pk}/new-version/")
        assert response.status_code == 201 and data(response)["version"] == 2

    def test_clone(self, api, theme_admin_a, org_a, make_theme):
        original = make_theme(org_a, "Corporate", status=ThemeStatus.ACTIVE)
        response = api(theme_admin_a).post(f"{LIST}{original.pk}/clone/", {"name": "Corporate Dark"})
        assert response.status_code == 201
        payload = data(response)
        assert payload["code"] == "CORPORATE_DARK" and payload["status"] == "draft" and payload["id"] != str(original.pk)

    def test_clone_into_a_foreign_organization_is_rejected(self, api, theme_admin_a, org_a, org_b, make_theme):
        original = make_theme(org_a, "Corporate", status=ThemeStatus.PUBLISHED)
        response = api(theme_admin_a).post(f"{LIST}{original.pk}/clone/", {"organization": str(org_b.pk)})
        assert response.status_code == 400


class TestPreviewEndpoint:
    def test_preview_of_a_stored_theme(self, api, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand", tokens={"light": {"primary": "#112233"}})
        payload = data(api(theme_admin_a).get(f"{LIST}{theme.pk}/preview/"))
        assert payload["variables"]["--color-primary"] == "#112233" and payload["publishable"] is True

    def test_preview_with_unsaved_tokens_stores_nothing(self, api, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        response = api(theme_admin_a).post(
            f"{LIST}{theme.pk}/preview/", {"tokens": {"light": {"primary": "#445566"}}, "mode": "light"}, content_type="application/json"
        )
        assert data(response)["variables"]["--color-primary"] == "#445566"
        theme.refresh_from_db()
        assert theme.tokens == {}

    def test_preview_reports_accessibility(self, api, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        response = api(theme_admin_a).post(
            f"{LIST}{theme.pk}/preview/", {"tokens": {"light": {"foreground": "#dddddd"}}}, content_type="application/json"
        )
        payload = data(response)
        assert payload["publishable"] is False
        assert any(c["level"] == "error" for c in payload["accessibility"])

    def test_preview_rejects_malicious_tokens(self, api, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        response = api(theme_admin_a).post(
            f"{LIST}{theme.pk}/preview/", {"tokens": {"light": {"primary": "url(javascript:alert(1))"}}}, content_type="application/json"
        )
        assert response.status_code == 400

    def test_members_cannot_preview(self, api, viewer_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand", status=ThemeStatus.PUBLISHED)
        assert api(viewer_a).get(f"{LIST}{theme.pk}/preview/").status_code == 403


class TestExportImportEndpoints:
    def test_export_import_roundtrip(self, api, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand", tokens={"light": {"primary": "#112233"}})
        client = api(theme_admin_a)
        document = data(client.get(f"{LIST}{theme.pk}/export/"))
        response = client.post(LIST + "import/", {"organization": str(org_a.pk), "document": document}, content_type="application/json")
        assert response.status_code == 201
        assert data(response)["tokens"] == {"light": {"primary": "#112233"}} and data(response)["status"] == "draft"

    def test_hostile_import_is_rejected(self, api, theme_admin_a, org_a):
        document = {"format": "checklist-platform.theme/1", "name": "x", "tokens": {"custom_css": "body{display:none}"}}
        response = api(theme_admin_a).post(LIST + "import/", {"organization": str(org_a.pk), "document": document}, content_type="application/json")
        assert response.status_code == 400 and not Theme.objects.exists()

    def test_members_cannot_import(self, api, viewer_a, org_a):
        response = api(viewer_a).post(LIST + "import/", {"organization": str(org_a.pk), "document": {}}, content_type="application/json")
        assert response.status_code == 403


class TestActiveEndpoint:
    def test_returns_the_resolved_look(self, api, viewer_a, org_a, make_theme):
        make_theme(org_a, "Brand", status=ThemeStatus.ACTIVE, tokens={"light": {"primary": "#112233"}}, default_appearance="dark")
        payload = data(api(viewer_a).get(LIST + "active/"))
        assert payload["source"] == "organization" and payload["mode"] == "dark"
        assert payload["theme"]["name"] == "Brand"
        assert payload["variables"]["light"]["--color-primary"] == "#112233"
        assert payload["stylesheet"].startswith("/themes/tokens.css?v=")
        assert set(payload["pwa"]["light"]) == {"theme_color", "background_color"}

    def test_default_when_nothing_is_active(self, api, viewer_a):
        payload = data(api(viewer_a).get(LIST + "active/"))
        assert payload["source"] == "default" and payload["theme"] is None

    def test_each_user_gets_their_own_organizations_theme(self, api, viewer_a, make_user, org_a, org_b, make_theme):
        make_theme(org_a, "A", status=ThemeStatus.ACTIVE)
        make_theme(org_b, "B", status=ThemeStatus.ACTIVE)
        assert data(api(viewer_a).get(LIST + "active/"))["theme"]["name"] == "A"
        assert data(api(make_user(organization=org_b)).get(LIST + "active/"))["theme"]["name"] == "B"


class TestAppearanceEndpoint:
    URL = "/api/v1/preferences/appearance/"

    def test_default(self, api, user):
        assert data(api(user).get(self.URL)) == {"mode": None, "effective": "system"}

    @pytest.mark.parametrize("mode", ["light", "dark", "system"])
    def test_set_each_mode(self, api, user, mode):
        response = api(user).put(self.URL, {"mode": mode}, content_type="application/json")
        assert response.status_code == 200 and data(response)["mode"] == mode
        assert UserAppearance.objects.get(user=user).mode == mode

    def test_null_clears(self, api, user):
        services.set_user_appearance(user, "dark")
        api(user).put(self.URL, {"mode": None}, content_type="application/json")
        assert not UserAppearance.objects.exists()

    def test_invalid_mode(self, api, user):
        assert api(user).put(self.URL, {"mode": "neon"}, content_type="application/json").status_code == 400

    def test_only_changes_the_callers_preference(self, api, make_user, org_a):
        one, two = make_user(organization=org_a), make_user(organization=org_a)
        api(one).put(self.URL, {"mode": "dark"}, content_type="application/json")
        assert not UserAppearance.objects.filter(user=two).exists()

    def test_reflected_in_the_active_endpoint(self, api, user):
        client = api(user)
        client.put(self.URL, {"mode": "dark"}, content_type="application/json")
        assert data(client.get(LIST + "active/"))["mode"] == "dark"


class TestDiscovery:
    def test_root_lists_theme_endpoints(self, client, db):
        endpoints = data(client.get("/api/v1/"))["endpoints"]
        assert {"themes", "active_theme", "appearance"} <= set(endpoints)

    def test_schema_documents_the_theme_endpoints(self, client, db):
        assert client.get("/api/schema/").status_code == 200
