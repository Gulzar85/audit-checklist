"""The theme engine must never become a CSS / HTML / JavaScript injection mechanism."""

from __future__ import annotations

import json
import re

import pytest

from apps.themes import css, services
from apps.themes import tokens as tk
from apps.themes.constants import ThemeStatus
from apps.themes.models import Theme

pytestmark = pytest.mark.django_db

INJECTIONS = [
    "<script>alert(1)</script>",
    "</style><script>alert(1)</script>",
    "javascript:alert(1)",
    "@import url(https://evil.example/x.css);",
    "url(https://evil.example/x.png)",
    "expression(alert(1))",
    "#fff; } body { display: none } .x {",
    "#fff /* */",
    "red",
    "var(--secret)",
    "calc(1px + 1px)",
    "\\0023 fff",
    "#fff\n#000",
    "<img src=x onerror=alert(1)>",
    "'; DROP TABLE themes_theme; --",
]

FORBIDDEN_FIELDS = ["custom_css", "custom_js", "html_template", "css", "js", "html", "script", "style"]


class TestColourInjection:
    @pytest.mark.parametrize("payload", INJECTIONS)
    def test_rejected_by_the_token_validator(self, payload):
        with pytest.raises(Exception):  # noqa: B017 - any validation error is a pass
            tk.validate_token_value("primary", payload)

    @pytest.mark.parametrize("payload", INJECTIONS)
    def test_rejected_by_the_service(self, org_a, payload):
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            services.create_theme(organization=org_a, name="x", tokens={"light": {"primary": payload}})
        assert not Theme.objects.exists()

    @pytest.mark.parametrize("payload", INJECTIONS)
    def test_rejected_by_the_api(self, client, theme_admin_a, org_a, payload):
        client.force_login(theme_admin_a)
        response = client.post(
            "/api/v1/themes/",
            {"organization": str(org_a.pk), "name": "x", "tokens": {"light": {"primary": payload}}},
            content_type="application/json",
        )
        assert response.status_code == 400 and not Theme.objects.exists()

    @pytest.mark.parametrize("payload", INJECTIONS)
    def test_rejected_by_the_editor(self, client, theme_admin_a, org_a, make_theme, payload):
        theme = make_theme(org_a, "Brand")
        client.force_login(theme_admin_a)
        form = client.get(f"/themes/{theme.pk}/edit/").context["form"]
        data = {n: f.initial for n, f in form.fields.items() if f.initial is not None}
        data.update({"name": "Brand", "default_appearance": "system", "light__primary": payload})
        client.post(f"/themes/{theme.pk}/edit/", data)
        theme.refresh_from_db()
        assert theme.tokens == {}


class TestFreeFormFieldsDoNotExist:
    @pytest.mark.parametrize("field", FORBIDDEN_FIELDS)
    def test_model_has_no_such_field(self, field):
        assert field not in {f.name for f in Theme._meta.get_fields()}

    @pytest.mark.parametrize("field", FORBIDDEN_FIELDS)
    def test_tokens_cannot_smuggle_them_in(self, org_a, field):
        from django.core.exceptions import ValidationError

        for document in ({field: "x"}, {"light": {field: "x"}}, {"shared": {field: "x"}}):
            with pytest.raises(ValidationError):
                services.create_theme(organization=org_a, name="x", tokens=document)

    def test_api_update_rejects_unknown_fields(self, client, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        client.force_login(theme_admin_a)
        response = client.patch(f"/api/v1/themes/{theme.pk}/", {"custom_css": "body{}"}, content_type="application/json")
        assert response.status_code == 400

    def test_editor_form_has_no_free_text_style_fields(self, org_a, make_theme):
        from apps.themes.forms import ThemeEditForm

        form = ThemeEditForm(theme=make_theme(org_a, "Brand"))
        free_text = [
            name for name, field in form.fields.items()
            if field.widget.__class__.__name__ in ("Textarea",) or name in FORBIDDEN_FIELDS
        ]
        assert free_text == []
        # The only text-like inputs are the colour pickers, which re-serialise what they accept.
        assert {f.widget.__class__.__name__ for f in form.fields.values()} <= {
            "TextInput", "ColorWidget", "NumberInput", "Select", "HiddenInput",
        }


class TestGeneratedStylesheet:
    def test_the_stylesheet_is_only_custom_properties(self, client, make_theme, org_a, make_user):
        make_theme(org_a, "Brand", status=ThemeStatus.ACTIVE, tokens={"light": {"primary": "#112233"}, "shared": {"font_family": "serif"}})
        client.force_login(make_user(organization=org_a))
        body = client.get("/themes/tokens.css").content.decode()
        body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
        declarations = [line.strip() for line in body.splitlines() if line.strip().endswith(";")]
        assert declarations
        assert all(re.fullmatch(r"(--[a-z0-9-]+|color-scheme): [^;{}<>@\\]+;", d) for d in declarations)
        for needle in ("<", ">", "@import", "url(", "expression", "javascript:", "\\"):
            assert needle not in body

    def test_only_known_selectors_and_one_media_query(self, db):
        stylesheet = css.compile_css(tk.default_tokens())
        selectors = re.findall(r"^\s*([^\s].*?)\s*\{$", stylesheet, flags=re.M)
        assert selectors == [
            ':root, [data-theme="light"]',
            '[data-theme="dark"]',
            "@media (prefers-color-scheme: dark)",
            '[data-theme="system"]',
        ]

    def test_font_choices_are_fixed_local_stacks(self):
        for _key, (_label, stack) in tk.FONT_STACKS.items():
            assert "url(" not in stack and "http" not in stack and "@" not in stack

    def test_no_remote_font_or_resource_can_be_referenced(self, db):
        stylesheet = css.compile_css(tk.default_tokens())
        assert "http" not in stylesheet and "//" not in stylesheet


class TestCspCompatibility:
    def test_pages_stay_strict(self, client, user):
        client.force_login(user)
        for path in ("/", "/themes/", "/themes/gallery/"):
            header = client.get(path)["Content-Security-Policy"]
            assert "'unsafe-inline'" not in header and "'unsafe-eval'" not in header, path

    def test_theme_stylesheet_is_a_same_origin_link_not_an_inline_style(self, client, user):
        client.force_login(user)
        html = client.get("/").content.decode()
        assert re.search(r'<link rel="stylesheet" href="/themes/tokens\.css\?v=[0-9a-f]+"', html)
        assert "<style" not in html and not re.search(r"\sstyle=", html)

    def test_editor_page_has_no_inline_script_or_style_attributes(self, client, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand")
        client.force_login(theme_admin_a)
        html = client.get(f"/themes/{theme.pk}/edit/").content.decode()
        assert not re.search(r"<script(?![^>]*\bsrc=)", html)
        assert not re.search(r"\sstyle=", html)
        assert not re.search(r"\son[a-z]+=", html)

    def test_frontend_code_applies_preview_through_the_cssom_with_a_name_allow_list(self):
        from django.conf import settings

        source = (settings.BASE_DIR / "static" / "js" / "components.js").read_text(encoding="utf-8")
        assert "setProperty" in source and r"/^--[a-z0-9-]+$/" in source
        assert "innerHTML" not in source and "eval(" not in source and "new Function" not in source


class TestExportDoesNotLeak:
    def test_export_has_no_internal_identifiers(self, client, theme_admin_a, org_a, make_theme):
        theme = make_theme(org_a, "Brand", status=ThemeStatus.ACTIVE, actor=theme_admin_a)
        client.force_login(theme_admin_a)
        raw = client.get(f"/themes/{theme.pk}/export/").content.decode()
        document = json.loads(raw)
        assert set(document) == {"format", "name", "code", "description", "default_appearance", "source_preset", "tokens"}
        for secret in (str(theme.pk), str(org_a.pk), theme_admin_a.email, str(theme_admin_a.pk), "csrf", "session"):
            assert secret not in raw


class TestTenantIsolation:
    def test_a_user_can_never_receive_another_organizations_tokens(self, client, make_theme, make_user, org_a, org_b):
        make_theme(org_b, "Secret brand", status=ThemeStatus.ACTIVE, tokens={"light": {"primary": "#123456"}})
        client.force_login(make_user(organization=org_a))
        assert "#123456" not in client.get("/themes/tokens.css").content.decode()
        assert "#123456" not in json.dumps(client.get("/api/v1/themes/active/").json())
        assert "Secret brand" not in client.get("/themes/").content.decode()
        assert "Secret brand" not in client.get("/api/v1/themes/").content.decode()

    def test_foreign_ids_are_indistinguishable_from_missing_ones(self, client, theme_admin_a, org_b, make_theme):
        theme = make_theme(org_b, "Foreign", status=ThemeStatus.PUBLISHED)
        client.force_login(theme_admin_a)
        missing = "00000000-0000-4000-8000-000000000000"
        for suffix in ("", "edit/", "export/"):
            assert client.get(f"/themes/{theme.pk}/{suffix}").status_code == client.get(f"/themes/{missing}/{suffix}").status_code == 404

    def test_activating_does_not_leak_across_scopes(self, org_a, org_b, make_theme):
        a = make_theme(org_a, "A", status=ThemeStatus.ACTIVE)
        make_theme(org_b, "B", status=ThemeStatus.ACTIVE)
        a.refresh_from_db()
        assert a.status == ThemeStatus.ACTIVE
