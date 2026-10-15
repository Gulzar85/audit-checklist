"""Cross-cutting UI checks: strict-CSP compatibility, accessibility structure, PWA shell.

These run over every Phase 1 page so a new template cannot quietly regress them.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest
from django.conf import settings
from django.urls import reverse

pytestmark = pytest.mark.django_db


@pytest.fixture
def pages(client, staff_user, org_a, make_type, make_location):
    """Rendered HTML for each Phase 1 page, as seen by platform staff (the richest view)."""
    root = make_location(org_a, "Root", code="ROOT")
    leaf = make_location(org_a, "Leaf", code="LEAF", parent=root, type=root.location_type)
    client.force_login(staff_user)
    urls = {
        "org list": reverse("organizations:list"),
        "org detail": reverse("organizations:detail", args=[org_a.pk]),
        "org create": reverse("organizations:create"),
        "org edit": reverse("organizations:update", args=[org_a.pk]),
        "org branding": reverse("organizations:branding", args=[org_a.pk]),
        "location list": reverse("locations:list"),
        "location tree": reverse("locations:tree"),
        "location detail": reverse("locations:detail", args=[root.pk]),
        "location detail leaf": reverse("locations:detail", args=[leaf.pk]),
        "location create": reverse("locations:create"),
        "location edit": reverse("locations:update", args=[leaf.pk]),
        "type list": reverse("locations:type_list"),
        "type create": reverse("locations:type_create"),
        "type edit": reverse("locations:type_update", args=[root.location_type_id]),
    }
    rendered = {}
    for name, url in urls.items():
        response = client.get(url)
        assert response.status_code == 200, name
        rendered[name] = response.content.decode()
    # HTMX fragments are HTML too.
    rendered["picker"] = client.get(reverse("locations:picker"), {"q": "root"}).content.decode()
    rendered["relations"] = client.get(reverse("locations:form_relations"), {"organization": str(org_a.pk)}).content.decode()
    rendered["tree panel"] = client.get(reverse("locations:tree"), HTTP_HX_REQUEST="true").content.decode()
    return rendered


class Collector(HTMLParser):
    """Collect what the structural checks need from one document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.text_stack: list[tuple[str, dict, list[str]]] = []
        self.buttons: list[tuple[dict, str]] = []
        self.links: list[tuple[dict, str]] = []
        self._open: list[tuple[str, dict, list[str]]] = []

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        if tag in ("button", "a"):
            self._open.append((tag, attributes, []))

    def handle_data(self, data):  # noqa: ANN001
        for _tag, _attrs, text in self._open:
            text.append(data)

    def handle_endtag(self, tag):  # noqa: ANN001
        if self._open and self._open[-1][0] == tag:
            _tag, attributes, text = self._open.pop()
            (self.buttons if tag == "button" else self.links).append((attributes, "".join(text).strip()))


def parse(html: str) -> Collector:
    collector = Collector()
    collector.feed(html)
    return collector


class TestRendering:
    def test_no_page_leaks_unrendered_template_syntax(self, pages):
        """A multi-line ``{# ... #}`` comment is not a comment in Django: it prints as text."""
        for name, html in pages.items():
            for token in ("{#", "#}", "{%", "%}", "{{", "}}"):
                assert token not in html, (name, token)

    def test_the_document_starts_with_a_doctype(self, pages):
        for name in ("org list", "location tree", "location create"):
            assert pages[name].lstrip().lower().startswith("<!doctype html>"), name


# ---------------------------------------------------------------------------
# Strict CSP
# ---------------------------------------------------------------------------
class TestStrictCspCompatibility:
    def test_no_inline_script_or_event_handlers(self, pages):
        for name, html in pages.items():
            for match in re.finditer(r"<script\b([^>]*)>", html):
                assert "src=" in match.group(1) or "nonce=" in match.group(1), (name, match.group(0))
            assert not re.search(r"\son(?:click|change|submit|load|input|keydown|focus|error)\s*=", html, re.I), name
            assert "javascript:" not in html.lower(), name

    def test_no_inline_style_attributes_or_style_blocks(self, pages):
        """CSP style-src has no 'unsafe-inline'; inline style="" would be blocked."""
        for name, html in pages.items():
            assert not re.search(r"<[a-z][^>]*\sstyle\s*=", html, re.I), name
            for match in re.finditer(r"<style\b([^>]*)>", html):
                assert "nonce=" in match.group(1), (name, match.group(0))

    def test_alpine_attributes_use_only_named_components(self, pages):
        """The Alpine CSP build cannot evaluate inline expressions (see ADR 0004)."""
        identifier = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
        attribute = re.compile(r'\s((?:x-(?:data|show|text|bind|if)|@[\w:.-]+|:[\w-]+))="([^"]*)"')
        for name, html in pages.items():
            for key, value in attribute.findall(html):
                assert identifier.match(value), f"{name}: {key}={value!r} is not CSP-build compatible"

    def test_no_x_model_which_the_csp_build_cannot_assign(self, pages):
        for name, html in pages.items():
            assert "x-model" not in html, name

    def test_every_alpine_component_used_is_registered(self, pages):
        registered = set(re.findall(r'Alpine\.data\("(\w+)"', (settings.BASE_DIR / "static/js/components.js").read_text()))
        for name, html in pages.items():
            for component in re.findall(r'x-data="([^"]+)"', html):
                assert component in registered, (name, component)

    def test_htmx_is_configured_for_the_csp(self, pages):
        meta = re.search(r'<meta name="htmx-config" content=\'([^\']+)\'', pages["location tree"])
        assert meta and '"allowEval": false' in meta.group(1) and '"includeIndicatorStyles": false' in meta.group(1)
        # HTMX would otherwise setAttribute("style") while settling a swap, which the CSP blocks
        # (seen in a real browser when the organization select re-scopes the picker).
        settle = re.search(r'"attributesToSettle": \[([^\]]*)\]', meta.group(1))
        assert settle and '"style"' not in settle.group(1)

    def test_components_load_before_alpine(self, pages):
        html = pages["location tree"]
        assert html.index("js/components.js") < html.index("vendor/alpine.min.js")


# ---------------------------------------------------------------------------
# Accessibility structure
# ---------------------------------------------------------------------------
class TestAccessibility:
    FULL_PAGES = [
        "org list", "org detail", "org create", "org edit", "org branding", "location list", "location tree",
        "location detail", "location create", "location edit", "type list", "type create", "type edit",
    ]

    def test_each_page_has_one_h1_a_main_landmark_and_a_skip_link(self, pages):
        for name in self.FULL_PAGES:
            html = pages[name]
            assert html.count("<h1") == 1, name
            assert 'id="main"' in html and 'href="#main"' in html, name
            assert '<html lang="en"' in html and "<title>" in html, name

    def test_every_button_and_link_has_an_accessible_name(self, pages):
        for name in self.FULL_PAGES + ["picker", "tree panel"]:
            document = parse(pages[name])
            for attributes, text in document.buttons + document.links:
                assert text or attributes.get("aria-label") or attributes.get("title"), (name, attributes)

    def test_icons_are_decorative(self, pages):
        for name, html in pages.items():
            for match in re.finditer(r"<i\b[^>]*data-lucide[^>]*>", html):
                assert 'aria-hidden="true"' in match.group(0), (name, match.group(0))

    def test_every_form_control_has_a_label(self, pages):
        for name in ("org create", "org edit", "org branding", "location create", "location edit", "type create", "type edit"):
            html = pages[name]
            labelled = set(re.findall(r'<label[^>]*\sfor="([^"]+)"', html))
            for tag, attributes in parse(html).tags:
                if tag in ("input", "select", "textarea") and attributes.get("type") not in ("hidden", "submit", "checkbox") and "name" in attributes:
                    if attributes["name"] == "csrfmiddlewaretoken":
                        continue
                    assert attributes.get("id") in labelled or attributes.get("aria-label"), (name, attributes.get("name"))

    def test_tables_have_captions_or_labels_and_scoped_headers(self, pages):
        for name in ("org list", "location list", "type list", "location detail"):
            html = pages[name]
            for table in re.findall(r"<table\b[^>]*>", html):
                assert "aria-label" in table, (name, table)
            for header in re.findall(r"<th\b[^>]*>", html):
                assert 'scope="col"' in header, (name, header)

    def test_status_is_not_conveyed_by_colour_alone(self, pages):
        assert "Active" in pages["location list"] and "Active" in pages["location tree"]
        for match in re.finditer(r'class="badge badge-(?:success|neutral|warning)[^"]*">(.*?)</span>', pages["location tree"], re.S):
            assert re.sub(r"<[^>]+>", "", match.group(1)).strip(), "badge without text"

    def test_errors_are_announced_and_linked_to_their_field(self, client, staff_user, org_a):
        client.force_login(staff_user)
        html = client.post(reverse("organizations:update", args=[org_a.pk]), {"name": "", "currency": "XX"}).content.decode()
        assert 'role="alert"' in html and 'aria-invalid="true"' in html and 'aria-describedby="error_id_name"' in html

    def test_dialogs_are_native_and_labelled(self, pages):
        for name in ("location detail", "org detail"):
            for dialog in re.findall(r"<dialog\b[^>]*>", pages[name]):
                assert "aria-labelledby" in dialog, (name, dialog)

    def test_decorative_empty_states_still_explain_themselves(self, client, staff_user):
        client.force_login(staff_user)
        assert "No locations yet" in client.get(reverse("locations:tree")).content.decode()

    def test_picker_exposes_combobox_semantics(self, pages):
        html = pages["location create"]
        assert 'role="combobox"' in html and 'aria-controls="id_parent_results"' in html and 'role="listbox"' in html


# ---------------------------------------------------------------------------
# Responsive / PWA shell
# ---------------------------------------------------------------------------
class TestPwaAndResponsive:
    def test_pages_are_mobile_first_and_installable(self, pages):
        for name in ("org list", "location tree", "location create"):
            html = pages[name]
            assert 'name="viewport"' in html and "width=device-width" in html
            assert 'rel="manifest"' in html and 'name="theme-color"' in html

    def test_mobile_navigation_exists_and_is_labelled(self, pages):
        html = pages["location tree"]
        assert 'id="mobile-nav"' in html and 'aria-controls="mobile-nav"' in html
        assert 'aria-label="Open navigation menu"' in html

    def test_service_worker_precaches_the_new_script_and_still_bypasses_api_and_admin(self, client):
        source = client.get(reverse("core:service-worker")).content.decode()
        assert "/static/js/components.js" in source and '"/api/"' in source and '"/admin/"' in source

    def test_new_static_assets_exist(self):
        assert (settings.BASE_DIR / "static/js/components.js").is_file()

    def test_tables_scroll_instead_of_overflowing_the_page(self, pages):
        assert "overflow-x-auto" in pages["location list"]

    def test_api_supports_offline_sync_friendly_fields(self, client, staff_user, org_a, make_location):
        """Stable ids and timestamps on every resource let a later offline engine sync by id + updated_at."""
        from rest_framework.test import APIClient

        make_location(org_a, "L")
        api = APIClient()
        api.force_authenticate(staff_user)
        item = api.get("/api/v1/locations/").json()["data"][0]
        assert re.fullmatch(r"[0-9a-f-]{36}", item["id"]) and item["updated_at"] and item["created_at"]
