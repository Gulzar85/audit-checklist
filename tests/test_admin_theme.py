"""Jazzmin configuration quality and the admin's Content-Security-Policy.

The admin is a different beast from the application UI: Jazzmin ships inline scripts,
`style=""` attributes and (by default) Google Fonts.  These tests pin down that it
(a) is branded and navigable like a product, (b) makes no third-party requests, and
(c) runs under a CSP that is only *narrowly* wider than the strict site policy - and
that any Jazzmin upgrade that changes an inline script fails loudly, here.
"""

from __future__ import annotations

import base64
import hashlib
import re

import pytest
from django.apps import apps
from django.conf import settings
from django.contrib import admin
from django.contrib.staticfiles import finders
from django.test import override_settings
from django.urls import NoReverseMatch, reverse

pytestmark = pytest.mark.django_db

JAZZMIN = settings.JAZZMIN_SETTINGS


@pytest.fixture
def staff_client(client, superuser):
    client.force_login(superuser)
    return client


@pytest.fixture
def admin_urls(org_a, make_location, superuser):
    """A broad set of admin screens (one of every template family in use)."""
    location = make_location(org_a, "L")
    return [
        "/admin/",
        "/admin/organizations/",
        "/admin/organizations/organization/",
        "/admin/organizations/organization/add/",
        f"/admin/organizations/organization/{org_a.pk}/change/",
        f"/admin/organizations/organization/{org_a.pk}/history/",
        "/admin/organizations/organization/import/",
        "/admin/organizations/organization/export/",
        "/admin/locations/location/",
        "/admin/locations/location/add/",
        f"/admin/locations/location/{location.pk}/change/",
        f"/admin/locations/locationtype/{location.location_type_id}/delete/",
        "/admin/locations/location/import/",
        "/admin/accounts/user/",
        "/admin/accounts/user/add/",
        f"/admin/accounts/user/{superuser.pk}/change/",
        f"/admin/accounts/user/{superuser.pk}/password/",
        "/admin/auth/group/add/",
        "/admin/password_change/",
        "/admin/password_change/done/",
        "/admin/core/activitylog/",
        "/admin/permissions/role/add/",
    ]


def inline_script_hashes(html: str) -> set[str]:
    bodies = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    return {
        "sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        for body in bodies
        if body.strip()
    }


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
class TestJazzminConfiguration:
    def test_no_third_party_requests_by_configuration(self):
        assert JAZZMIN["use_google_fonts_cdn"] is False

    def test_the_related_object_popup_is_off_because_framing_is_denied(self):
        assert JAZZMIN["related_modal_active"] is False and settings.X_FRAME_OPTIONS == "DENY"

    def test_developer_only_widgets_are_off(self):
        assert not JAZZMIN["show_ui_builder"] and not JAZZMIN["show_theme_chooser"]

    def test_no_stock_avatar_fallback(self):
        """Jazzmin would show a stock *photo* for users without an avatar."""
        assert "user_avatar" not in JAZZMIN

    def test_branding_assets_exist_and_are_self_hosted(self):
        for key in ("site_logo", "site_icon", "custom_css", "custom_js"):
            assert finders.find(JAZZMIN[key]), f"{key} -> {JAZZMIN[key]} is not a static file"
            assert not JAZZMIN[key].startswith(("http://", "https://", "//"))

    def test_brand_stylesheet_references_no_external_resources(self):
        css = finders.find(JAZZMIN["custom_css"])
        text = open(css, encoding="utf-8").read()  # noqa: SIM115
        assert "@import" not in text and not re.search(r"url\(\s*['\"]?(?:https?:)?//", text)

    def test_every_registered_admin_model_has_an_icon(self):
        icons = {key.lower() for key in JAZZMIN["icons"]}
        missing = [
            model._meta.label for model in admin.site._registry if model._meta.label.lower() not in icons
        ]
        assert not missing, f"add icons for: {missing}"

    def test_every_app_with_admin_models_has_an_icon(self):
        icons = {key.lower() for key in JAZZMIN["icons"]}
        labels = {model._meta.app_label for model in admin.site._registry}
        assert labels <= icons, labels - icons

    def test_icon_classes_are_valid_font_awesome_names(self):
        css = open(finders.find("vendor/fontawesome-free/css/all.min.css"), encoding="utf-8").read()  # noqa: SIM115
        for label, icon in JAZZMIN["icons"].items():
            name = icon.split()[-1].removeprefix("fa-")
            assert f".fa-{name}" in css, (label, icon)

    def test_sidebar_ordering_references_real_apps_and_models(self):
        for entry in JAZZMIN["order_with_respect_to"]:
            if "." in entry:
                app_label, model_name = entry.split(".")
                assert apps.get_app_config(app_label).get_model(model_name), entry
            else:
                assert apps.get_app_config(entry), entry

    def test_menu_links_resolve(self):
        for link in [*JAZZMIN["topmenu_links"], *JAZZMIN["usermenu_links"]]:
            url = link["url"]
            if url.startswith("/"):
                continue
            try:
                reverse(url)
            except NoReverseMatch:  # pragma: no cover - the assertion message is the point
                pytest.fail(f"menu link {link['name']!r} -> {url!r} does not resolve")

    def test_application_pages_are_reachable_from_the_admin_menu(self):
        urls = {link["url"] for link in JAZZMIN["topmenu_links"]}
        assert {"core:home", "organizations:list", "locations:tree"} <= urls

    def test_ui_tweaks_use_only_adminlte_4_options(self):
        """AdminLTE 3 colour classes do not exist in the bundled AdminLTE 4 stylesheet."""
        tweaks = settings.JAZZMIN_UI_TWEAKS
        assert not {"accent", "navbar", "sidebar", "brand_colour"} & set(tweaks)
        assert tweaks["default_theme_mode"] in {"light", "dark", "auto"}
        adminlte = open(finders.find("vendor/adminlte/css/adminlte.min.css"), encoding="utf-8").read()  # noqa: SIM115
        assert "sidebar-dark-primary" not in adminlte  # the reason those keys were removed

    def test_jazzmin_is_pinned_so_csp_hashes_cannot_drift_silently(self):
        text = (settings.BASE_DIR / "requirements" / "base.txt").read_text(encoding="utf-8")
        assert re.search(r"django-jazzmin>=3\.0\.\d+,<3\.1", text)

    def test_jazzmin_precedes_the_admin_app(self):
        apps_list = settings.INSTALLED_APPS
        assert apps_list.index("jazzmin") < apps_list.index("django.contrib.admin")

    def test_the_search_box_targets_one_model(self):
        assert JAZZMIN["search_model"] == ["locations.Location"]


# ---------------------------------------------------------------------------
# Rendered pages
# ---------------------------------------------------------------------------
class TestRenderedAdmin:
    def test_every_page_renders(self, staff_client, admin_urls):
        for url in admin_urls:
            assert staff_client.get(url).status_code == 200, url

    def test_no_page_requests_a_third_party_origin(self, staff_client, admin_urls):
        for url in admin_urls + ["/admin/login/"]:
            html = staff_client.get(url, follow=True).content.decode()
            external = re.findall(r'(?:src|href|action)="((?:https?:)?//[^"]+)"', html)
            assert not external, (url, external)
            assert "fonts.googleapis.com" not in html and "fonts.gstatic.com" not in html, url

    def test_anonymous_login_page_is_self_contained_and_branded(self, client):
        html = client.get("/admin/login/").content.decode()
        assert "fonts.googleapis.com" not in html
        assert "icons/icon-192.png" in html and "AdminLTELogo" not in html

    def test_branding_replaces_the_stock_adminlte_assets(self, staff_client):
        html = staff_client.get("/admin/").content.decode()
        assert "icons/icon-192.png" in html and "AdminLTELogo" not in html and "user2-160x160" not in html
        assert "css/admin-brand.css" in html and "js/admin-compat.js" in html
        assert "Checklist Platform Admin" in html

    def test_navigation_links_back_to_the_application(self, staff_client):
        html = staff_client.get("/admin/").content.decode()
        for path in (reverse("core:home"), reverse("organizations:list"), reverse("locations:tree")):
            assert f'href="{path}"' in html

    def test_sidebar_groups_follow_the_configured_order(self, staff_client):
        html = staff_client.get("/admin/").content.decode()
        order = [html.index(f">{name}<") for name in ("Organizations", "Locations", "Accounts")]
        assert order == sorted(order)

    def test_global_search_is_scoped_to_locations(self, staff_client):
        html = staff_client.get("/admin/").content.decode()
        assert html.count('type="search"') == 1 and "Search Locations" in html

    def test_organization_page_manages_members_inline(self, staff_client, org_a):
        html = staff_client.get(f"/admin/organizations/organization/{org_a.pk}/change/").content.decode()
        assert "memberships-TOTAL_FORMS" in html and re.search(r">\s*Members\s*<", html)

    def test_members_can_be_added_from_the_organization_page(self, staff_client, org_a, user):
        from apps.organizations.models import OrganizationMembership

        data = {
            "name": org_a.name, "slug": org_a.slug, "memberships-TOTAL_FORMS": "1",
            "memberships-INITIAL_FORMS": "0", "memberships-MIN_NUM_FORMS": "0", "memberships-MAX_NUM_FORMS": "1000",
            "memberships-0-user": str(user.pk), "memberships-0-status": "active", "_save": "Save",
        }
        response = staff_client.post(f"/admin/organizations/organization/{org_a.pk}/change/", data)
        assert response.status_code == 302
        assert OrganizationMembership.objects.filter(user=user, organization=org_a, is_active=True).exists()


# ---------------------------------------------------------------------------
# Content-Security-Policy
# ---------------------------------------------------------------------------
def directive(header: str, name: str) -> list[str]:
    for part in header.split(";"):
        tokens = part.split()
        if tokens and tokens[0] == name:
            return tokens[1:]
    return []


class TestAdminContentSecurityPolicy:
    def test_admin_pages_carry_an_enforced_policy(self, staff_client):
        assert "Content-Security-Policy" in staff_client.get("/admin/")

    def test_scripts_are_allowed_only_by_hash_never_by_unsafe_inline(self, staff_client):
        header = staff_client.get("/admin/")["Content-Security-Policy"]
        scripts = directive(header, "script-src")
        assert "'unsafe-inline'" not in scripts and "'unsafe-eval'" not in scripts
        for digest in settings.ADMIN_CSP_SCRIPT_HASHES:
            assert f"'{digest}'" in scripts

    def test_only_style_attributes_may_be_inline(self, staff_client):
        header = staff_client.get("/admin/")["Content-Security-Policy"]
        assert directive(header, "style-src-attr") == ["'unsafe-inline'"]
        assert "'unsafe-inline'" not in directive(header, "style-src")
        assert "'unsafe-inline'" not in directive(header, "style-src-elem")

    def test_no_third_party_origin_is_ever_allowed(self, staff_client):
        header = staff_client.get("/admin/")["Content-Security-Policy"]
        assert not re.search(r"https?://|\*", header)

    def test_the_rest_of_the_site_keeps_the_strict_policy(self, client, staff_client):
        for url in ("/", "/api/v1/", reverse("organizations:list"), reverse("locations:tree")):
            header = staff_client.get(url)["Content-Security-Policy"]
            assert "sha256-" not in header and not directive(header, "style-src-attr"), url
            assert "'unsafe-inline'" not in header

    def test_login_page_gets_the_admin_policy_too(self, client):
        header = client.get("/admin/login/")["Content-Security-Policy"]
        assert any(f"'{d}'" in header for d in settings.ADMIN_CSP_SCRIPT_HASHES)

    def test_every_inline_script_is_covered_by_a_hash(self, staff_client, client, admin_urls):
        """If Jazzmin changes an inline script this fails and names the digest to review."""
        allowed = set(settings.ADMIN_CSP_SCRIPT_HASHES)
        seen: set[str] = set()
        for url in admin_urls:
            found = inline_script_hashes(staff_client.get(url).content.decode())
            assert found <= allowed, f"{url}: new inline script(s) {sorted(found - allowed)}"
            seen |= found
        seen |= inline_script_hashes(client.get("/admin/login/").content.decode())
        # ...and nothing stale: every allowed hash is still in use.
        assert allowed <= seen, f"unused hashes (remove them): {sorted(allowed - seen)}"

    def test_no_inline_style_elements_or_event_handlers(self, staff_client, admin_urls):
        for url in admin_urls:
            html = staff_client.get(url).content.decode()
            assert not re.search(r"<style\b", html), url
            assert not re.search(r"\son(?:click|change|submit|load|error|focus)\s*=", html, re.I), url
            assert "javascript:" not in re.sub(r"<script.*?</script>", "", html, flags=re.S).lower(), url

    @override_settings(SECURE_CSP={}, SECURE_CSP_REPORT_ONLY=settings.BASE_CSP)
    def test_report_only_environments_get_a_report_only_admin_policy(self, staff_client):
        """Development runs the CSP report-only; the admin variant follows the same mode."""
        response = staff_client.get("/admin/")
        assert "Content-Security-Policy" not in response
        header = response["Content-Security-Policy-Report-Only"]
        assert "sha256-" in header and directive(header, "style-src-attr") == ["'unsafe-inline'"]

    def test_the_policy_builder_never_mutates_the_base_policy(self):
        from apps.core.middleware import admin_policy

        before = {k: list(v) for k, v in settings.BASE_CSP.items()}
        admin_policy(settings.BASE_CSP)
        assert settings.BASE_CSP == before and "style-src-attr" not in settings.BASE_CSP

    def test_non_admin_paths_are_untouched_by_the_middleware(self, client):
        header = client.get("/health/")["Content-Security-Policy"]
        assert "sha256-" not in header
