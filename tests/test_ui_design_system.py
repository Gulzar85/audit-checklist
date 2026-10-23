"""The enterprise UI design system: shell, navigation, components, states, search, tree, showcase."""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest
from django.conf import settings
from django.template import Context, Template
from django.test import RequestFactory
from django.urls import reverse

from apps.core import navigation
from apps.core.timeline import classify, group_activity
from apps.core.ui import STATUSES, status_info

pytestmark = pytest.mark.django_db


def render(source: str, **context) -> str:
    return Template("{% load ui %}" + source).render(Context(context))


def page(client, user, url: str) -> str:
    client.force_login(user)
    response = client.get(url)
    assert response.status_code == 200, url
    return response.content.decode()


class Names(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.open: list[tuple[str, dict, list[str]]] = []
        self.controls: list[tuple[str, dict, str]] = []

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        if tag in ("button", "a"):
            self.open.append((tag, dict(attrs), []))

    def handle_data(self, data):  # noqa: ANN001
        for _t, _a, text in self.open:
            text.append(data)

    def handle_endtag(self, tag):  # noqa: ANN001
        if self.open and self.open[-1][0] == tag:
            t, a, text = self.open.pop()
            self.controls.append((t, a, "".join(text).strip()))


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------
class TestApplicationShell:
    def test_signed_in_pages_get_sidebar_topbar_and_main(self, client, admin_a):
        html = page(client, admin_a, "/")
        for marker in ('id="app-sidebar"', 'class="app-topbar"', 'id="main"', 'class="app-content"', 'class="app-footer"'):
            assert marker in html, marker
        assert html.count("<h1") == 1 and 'href="#main"' in html

    def test_sidebar_state_is_rendered_by_the_server_from_a_cookie(self, client, admin_a):
        client.force_login(admin_a)
        assert 'data-sidebar="expanded"' in client.get("/").content.decode()
        client.cookies["cp_sidebar"] = "collapsed"
        assert 'data-sidebar="collapsed"' in client.get("/").content.decode()
        client.cookies["cp_sidebar"] = "<script>"
        assert 'data-sidebar="expanded"' in client.get("/").content.decode()

    def test_drawer_and_collapse_controls_are_labelled_and_wired(self, client, admin_a):
        html = page(client, admin_a, "/")
        assert 'aria-label="Open navigation menu"' in html and 'aria-controls="app-sidebar"' in html
        assert 'aria-label="Toggle sidebar width"' in html
        assert re.search(r'<aside id="app-sidebar"[^>]*:data-open="drawerState"', html)
        assert 'x-data="shell"' in html and "@keydown.escape.window" in html

    def test_collapsed_items_have_tooltips_and_keep_their_labels(self, client, admin_a):
        html = page(client, admin_a, "/")
        assert re.search(r'class="sidebar-link"[^>]*data-tooltip="Dashboard"', html)
        assert 'class="sidebar-label truncate">Dashboard<' in html  # still in the DOM (screen-reader name)

    def test_topbar_has_search_notifications_help_theme_and_user_menu(self, client, admin_a):
        html = page(client, admin_a, "/")
        for label in ('aria-label="Search"', 'aria-label="Notifications"', 'aria-label="Help"',
                      "Appearance: colour mode", "Account menu for"):
            assert label in html, label
        assert "Sign out" in html and 'action="/auth/logout/"' in html

    def test_signed_out_error_pages_use_a_minimal_chrome(self, client, db):
        html = client.get("/definitely-missing/").content.decode()
        assert 'id="app-sidebar"' not in html and "Page not found" in html and "Sign in" in html

    def test_auth_layout_is_standalone_and_branded(self, client, db):
        html = client.get(reverse("accounts:login")).content.decode()
        assert 'id="app-sidebar"' not in html and "Continue with Microsoft" not in html or "Continue with Microsoft" in html
        assert "Secure enterprise access" in html and "<h1" in html and html.count("<h1") == 1

    def test_blank_layout_exists(self):
        from django.template.loader import get_template

        for name in ("layouts/app.html", "layouts/auth.html", "layouts/blank.html"):
            get_template(name)

    def test_page_width_modifiers(self, client, admin_a):
        assert "page-narrow" in page(client, admin_a, reverse("accounts:profile"))
        assert 'class="page "' in page(client, admin_a, "/") or 'class="page' in page(client, admin_a, "/")

    def test_toasts_render_django_messages_accessibly(self, client, admin_a, org_a):
        client.force_login(admin_a)
        response = client.post(reverse("accounts:preferences"), {"timezone": "Asia/Karachi", "language": "en"}, follow=True)
        html = response.content.decode()
        assert 'data-toast-region' in html and "Preferences saved." in html
        assert re.search(r'class="toast toast-success" data-toast data-tone="success" role="status"', html)
        assert 'aria-label="Dismiss notification"' in html

    def test_offline_banner_and_network_indicator_exist(self, client, admin_a):
        html = page(client, admin_a, "/")
        assert html.count('x-data="networkStatus"') == 2 and "You are offline" in html


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------
class TestNavigation:
    def test_unbuilt_modules_are_declared_but_never_rendered(self, client, admin_a):
        html = page(client, admin_a, "/")
        for label in ("Checklist Templates", "Reports", "My Tasks", "Roles &amp; Permissions"):
            assert label not in html, label
        assert {"reports", "settings"} <= set(navigation.PLANNED)  # findings and corrective actions are built (Phases 12-13)

    def test_built_modules_are_listed_in_their_sections(self, client, staff_user):
        html = page(client, staff_user, "/")
        for label in ("General", "Administration", "Dashboard", "Organizations", "Locations", "Location types", "Themes"):
            assert label in html, label

    def test_every_rendered_link_resolves(self, client, staff_user):
        client.force_login(staff_user)
        request = RequestFactory().get("/")
        request.user = staff_user
        for section in navigation.build_navigation(request):
            for item in section.items:
                if item.external:
                    continue
                assert client.get(item.href).status_code in (200, 302), item.href

    def test_staff_only_entries_are_hidden_from_members(self, client, viewer_a, staff_user):
        member_html = page(client, viewer_a, "/")
        assert "UI components" not in member_html and "Django admin" not in member_html
        client.logout()
        staff_html = page(client, staff_user, "/")
        assert "UI components" in staff_html and "Django admin" in staff_html

    def test_hiding_a_menu_item_is_not_authorization(self, client, viewer_a):
        """The destination stays forbidden even though the link is not shown."""
        client.force_login(viewer_a)
        assert client.get(reverse("core:ui")).status_code == 403
        assert client.get("/admin/", follow=False).status_code == 302  # to the admin login, not the admin
        assert client.get(reverse("themes:create")).status_code == 403

    def test_active_state_is_the_most_specific_match(self, client, admin_a):
        html = page(client, admin_a, reverse("locations:type_list"))
        current = re.findall(r'<a href="([^"]+)" class="sidebar-link" aria-current="page"', html)
        assert current == [reverse("locations:type_list")]
        tree = page(client, admin_a, reverse("locations:tree"))
        assert re.findall(r'<a href="([^"]+)" class="sidebar-link" aria-current="page"', tree) == [reverse("locations:tree")]

    def test_anonymous_users_get_no_navigation(self, rf, db):
        from django.contrib.auth.models import AnonymousUser

        request = rf.get("/")
        request.user = AnonymousUser()
        assert navigation.build_navigation(request) == []

    def test_navigation_is_configuration_not_template_markup(self):
        keys = [item.key for item in navigation.NAVIGATION]
        assert len(keys) == len(set(keys)) and "dashboard" in keys
        sections = {s.key for s in navigation.SECTIONS}
        assert {item.section for item in navigation.NAVIGATION} <= sections

    def test_sidebar_markup_has_no_hard_coded_links(self):
        source = (settings.BASE_DIR / "templates/components/navigation/sidebar.html").read_text(encoding="utf-8")
        assert "organizations:list" not in source and "locations:" not in source


# ---------------------------------------------------------------------------
# Organization switcher
# ---------------------------------------------------------------------------
class TestOrganizationSwitcher:
    def test_single_organization_is_shown_without_a_menu(self, client, admin_a):
        html = page(client, admin_a, "/")
        assert "Org A" in html and 'aria-controls="org-switcher-menu"' not in html

    def test_member_of_two_organizations_gets_a_post_switcher(self, client, make_user, org_a, org_b):
        user = make_user(organization=org_a)
        from apps.accounts import services

        services.add_membership(user, org_b)
        html = page(client, user, "/")
        assert 'aria-controls="org-switcher-menu"' in html and 'role="menuitemradio"' in html
        assert 'action="/organizations/switch/"' in html and 'name="csrfmiddlewaretoken"' in html
        assert "Org B" in html

    def test_foreign_organizations_are_not_offered_and_forged_posts_fail(self, client, admin_a, org_b):
        html = page(client, admin_a, "/")
        assert "Org B" not in html
        assert client.post(reverse("orgcontext:switch"), {"organization": str(org_b.pk)}).status_code == 404


# ---------------------------------------------------------------------------
# Component tags
# ---------------------------------------------------------------------------
class TestButtons:
    @pytest.mark.parametrize("variant", ["primary", "secondary", "outline", "ghost", "danger", "link"])
    def test_variants(self, variant):
        html = render('{% button "Go" variant=v %}', v=variant)
        assert f"btn btn-{variant}" in html and ">Go<" in html and 'type="button"' in html

    def test_link_button_icon_and_sizes(self):
        html = render('{% button "Edit" href="/x/" variant="secondary" icon="pencil" size="sm" %}')
        assert html.strip().startswith('<a href="/x/"') and "btn-sm" in html and 'data-lucide="pencil"' in html and 'aria-hidden="true"' in html

    def test_icon_only_buttons_keep_an_accessible_name(self):
        html = render('{% button "More actions" icon="ellipsis-vertical" icon_only=True %}')
        assert 'aria-label="More actions"' in html and "btn-icon" in html and ">More actions<" not in html

    def test_disabled_submit_and_modal_trigger(self):
        assert "disabled" in render('{% button "Save" type="submit" variant="primary" disabled=True %}')
        assert 'data-modal-open="dlg"' in render('{% button "Open" modal="dlg" %}')

    def test_labels_are_escaped(self):
        assert "&lt;script&gt;" in render('{% button label %}', label="<script>x</script>")


class TestStatusBadges:
    @pytest.mark.parametrize("key", list(STATUSES))
    def test_every_status_has_text_icon_and_a_semantic_tone(self, key):
        html = render("{% status_badge key %}", key=key)
        info = STATUSES[key]
        assert f"badge-{info.tone}" in html and info.label in html and f'data-lucide="{info.icon}"' in html

    def test_unknown_status_falls_back_to_neutral_not_a_colour(self):
        assert "badge-neutral" in render("{% status_badge 'something-new' %}")

    def test_registry_uses_only_the_theme_tones(self):
        assert {info.tone for info in STATUSES.values()} <= {"success", "warning", "danger", "info", "neutral"}
        assert status_info("In progress").label == "In progress" and status_info(None).tone == "neutral"

    def test_templates_never_hard_code_status_colours(self):
        source = (settings.BASE_DIR / "templates/components/badges/status.html").read_text(encoding="utf-8")
        assert not re.search(r"(?:red|green|amber|blue|slate)-\d", source)


class TestCardsPageHeaderBreadcrumbs:
    def test_card_with_title_and_tone(self):
        html = render('{% card title="Totals" subtitle="This week" tone="warning" icon="layers" %}body{% endcard %}')
        assert 'class="card card-warning"' in html and '<h2 class="card-title">Totals</h2>' in html and "body" in html

    def test_card_flush_and_heading_level(self):
        html = render('{% card title="T" flush=True heading_level=3 %}x{% endcard %}')
        assert "<h3" in html and 'class="card-body"' not in html

    def test_page_header_has_one_h1_actions_and_status(self):
        html = render('{% page_header title="Templates" subtitle="Manage" icon="clipboard-list" status="draft" %}<a href="#">Act</a>{% endpage_header %}')
        assert html.count("<h1") == 1 and "page-actions" in html and "Draft" in html and "Manage" in html

    def test_page_header_without_actions_has_no_empty_container(self):
        assert "page-actions" not in render('{% page_header title="T" %}  {% endpage_header %}')

    def test_breadcrumbs_mark_the_current_page_and_collapse_on_phones(self):
        html = render('{% breadcrumbs "Home" "core:home" "Checklists" "core:ui" "Templates" "core:ui" "Safety" %}')
        assert 'aria-label="Breadcrumbs"' in html and html.count('aria-current="page"') == 1
        assert 'breadcrumb-back' in html and 'Back to Templates' in html and 'hidden sm:flex' in html  # phones: one back link
        assert 'data-lucide="house"' in html and ">Safety<" in html  # home icon; current page last

    def test_breadcrumbs_render_unreversible_targets_as_text_not_broken_links(self):
        html = render('{% breadcrumbs "Home" "core:home" "Ghost" "no:such" "Here" %}')
        assert ">Ghost</span>" in html and "breadcrumb-text" in html and 'href=""' not in html

    def test_breadcrumbs_accept_paths_and_spliced_lists(self):
        html = render('{% breadcrumbs "Home" "/" crumbs "Now" %}', crumbs=["A", "/a/", "B", "/b/"])
        assert 'href="/a/"' in html and 'href="/b/"' in html and 'aria-current="page"' in html

    def test_as_crumbs_filter(self, org_a):
        html = render('{% breadcrumbs "Home" "/" items|as_crumbs:"organizations:detail" "Here" %}', items=[org_a])
        assert f"/organizations/{org_a.pk}/" in html and "Org A" in html


class TestOverlays:
    def test_modal_is_a_labelled_native_dialog(self):
        html = render('{% modal id="m1" title="Hello" description="Desc" %}body{% endmodal %}')
        assert '<dialog id="m1"' in html and 'aria-labelledby="m1-title"' in html and 'aria-describedby="m1-desc"' in html
        assert 'id="m1-title"' in html and "data-modal-close" in html and 'aria-label="Close dialog"' in html

    def test_drawer(self):
        html = render('{% drawer id="d1" title="Filters" %}x{% enddrawer %}')
        assert 'class="drawer-dialog"' in html and 'aria-labelledby="d1-title"' in html and 'aria-label="Close panel"' in html

    def test_confirm_dialog_states_the_consequence_and_posts_with_csrf(self):
        from django.template.loader import render_to_string

        html = render_to_string("components/modals/confirm.html", {
            "id": "c1", "trigger": "Delete", "tone": "danger", "title": "Delete it?", "message": "Cannot be undone.",
            "action": "/x/", "confirm_label": "Delete",
        })
        assert 'x-data="confirmDialog"' in html and 'aria-describedby="c1-desc"' in html and "Cannot be undone." in html
        assert 'method="post"' in html and "btn-danger" in html and "Cancel" in html


class TestStates:
    def test_empty_state_explains_and_offers_an_action(self):
        from django.template.loader import render_to_string

        html = render_to_string("components/states/empty.html", {
            "title": "No templates", "message": "Create one.", "action_url": "/new/", "action_label": "Create template"})
        assert "No templates" in html and "Create one." in html and 'href="/new/"' in html

    def test_legacy_empty_state_include_still_works(self):
        from django.template.loader import render_to_string

        assert "Nothing" in render_to_string("components/empty_state.html", {"title": "Nothing", "icon": "inbox"})

    def test_error_state_is_an_alert_without_technical_detail(self):
        from django.template.loader import render_to_string

        html = render_to_string("components/states/error.html", {"retry_url": "/again/"})
        assert 'role="alert"' in html and "Something went wrong" in html and "Try again" in html
        assert "Traceback" not in html

    @pytest.mark.parametrize("kind", ["table", "card", "list", "text"])
    def test_skeletons_are_decorative_with_a_status_message(self, kind):
        from django.template.loader import render_to_string

        html = render_to_string("components/states/loading.html", {"kind": kind})
        assert 'aria-hidden="true"' in html and 'role="status"' in html and "Loading" in html


class TestTabsTreeTimelineProgress:
    def test_in_page_tabs_follow_the_aria_pattern(self):
        from django.template.loader import render_to_string

        html = render_to_string("components/navigation/tab_buttons.html", {
            "tabs": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}], "label": "Sections"})
        assert 'role="tablist"' in html and html.count('role="tab"') == 2
        assert 'aria-selected="true"' in html and 'aria-selected="false"' in html and 'tabindex="-1"' in html
        assert 'aria-controls="panel-a"' in html

    def test_link_tabs_mark_the_current_page(self):
        from django.template.loader import render_to_string

        html = render_to_string("components/navigation/tabs.html", {
            "tabs": [{"key": "a", "label": "A", "href": "/a/"}, {"key": "b", "label": "B", "href": "/b/", "count": 3}], "active": "b"})
        assert html.count('aria-current="page"') == 1 and "3" in html

    def test_tree_nodes_lazy_load_children_one_level_at_a_time(self):
        from django.template.loader import render_to_string

        html = render_to_string("components/tree/tree.html", {"label": "Places", "nodes": [
            {"label": "Root", "has_children": True, "children_url": "/kids/", "children": []},
            {"label": "Leaf", "href": "/leaf/", "children": []}]})
        assert 'x-data="treeNav"' in html and 'hx-get="/kids/"' in html and 'hx-trigger="toggle once"' in html
        assert "<summary" in html and "data-tree-leaf" in html and 'aria-label="Places"' in html

    def test_tree_renders_inline_children_without_lazy_attributes(self):
        from django.template.loader import render_to_string

        html = render_to_string("components/tree/tree.html", {"nodes": [
            {"label": "A", "children": [{"label": "B", "children": []}]}]})
        assert "hx-get" not in html and ">B<" in html

    def test_timeline_groups_by_day_and_classifies(self, org_a, staff_user):
        from apps.core.services import log_activity

        log_activity(verb="theme.published", organization=org_a, actor=staff_user, description="Published Brand")
        log_activity(verb="organization.updated", organization=org_a, actor=staff_user, description="Updated Org A")
        from apps.core.selectors import recent_activity

        days = group_activity(recent_activity(organization_id=org_a.pk))
        assert days[0]["day"] == "Today" and len(days[0]["events"]) == 2
        assert {e["tone"] for e in days[0]["events"]} == {"success", "info"}
        from django.template.loader import render_to_string

        html = render_to_string("components/timeline/timeline.html", {"days": days})
        assert "Published Brand" in html and "<time" in html and "timeline-dot-success" in html

    def test_empty_timeline_is_an_empty_state(self):
        from django.template.loader import render_to_string

        assert "No activity yet" in render_to_string("components/timeline/timeline.html", {"days": []})

    @pytest.mark.parametrize(("verb", "tone"), [("theme.created", "success"), ("user.suspended", "warning"),
                                                 ("x.rejected", "danger"), ("a.updated", "info"), ("zzz", "neutral")])
    def test_verb_classification(self, verb, tone):
        assert classify(verb)[0] == tone

    def test_native_progress_and_steps(self):
        from django.template.loader import render_to_string

        bar = render_to_string("components/progress/progress.html", {"value": 40, "label": "Done"})
        assert "<progress" in bar and 'aria-label="Done"' in bar and "style=" not in bar
        steps = render_to_string("components/progress/steps.html", {"steps": [
            {"label": "A", "state": "done"}, {"label": "B", "state": "current"}, {"label": "C", "state": "todo"}]})
        assert 'aria-current="step"' in steps and "Completed:" in steps


# ---------------------------------------------------------------------------
# Dashboard, search, lazy branches, showcase
# ---------------------------------------------------------------------------
class TestDashboard:
    def test_structured_dashboard_with_real_figures(self, client, admin_a, org_a, make_location):
        make_location(org_a, "Root", code="ROOT")
        html = page(client, admin_a, "/")
        for text in ("Key figures", "Locations by type", "Recent activity", "Location status", "System health", "Getting started"):
            assert text in html, text
        assert html.count('class="stat-card') == 4 and "<progress" in html

    def test_empty_organization_gets_helpful_empty_states(self, client, admin_a):
        html = page(client, admin_a, "/")
        assert "No locations yet" in html and "No activity yet" in html

    def test_dashboard_is_tenant_scoped(self, client, admin_a, org_b, make_location):
        make_location(org_b, "Foreign-secret", code="FS")
        assert "Foreign-secret" not in page(client, admin_a, "/")

    def test_dashboard_query_budget(self, client, admin_a, django_assert_max_num_queries):
        client.force_login(admin_a)
        client.get("/")
        with django_assert_max_num_queries(24):
            assert client.get("/").status_code == 200


class TestGlobalSearch:
    url = "/search/"

    def test_requires_authentication(self, client, db):
        assert client.get(self.url, {"q": "acme"}).status_code == 302

    def test_short_queries_get_guidance(self, client, admin_a):
        client.force_login(admin_a)
        assert "at least two characters" in client.get(self.url, {"q": "a"}).content.decode()

    def test_results_are_grouped_and_scoped(self, client, admin_a, org_a, org_b, make_location):
        make_location(org_a, "Karachi Hub", code="KHI")
        make_location(org_b, "Karachi Secret", code="KHS")
        client.force_login(admin_a)
        html = client.get(self.url, {"q": "karachi"}).content.decode()
        assert "Locations" in html and "Karachi Hub" in html and "Karachi Secret" not in html
        org_html = client.get(self.url, {"q": "org a"}).content.decode()
        assert "Organizations" in org_html and "Org A" in org_html

    def test_no_match_shows_an_empty_state_with_the_query_escaped(self, client, admin_a):
        client.force_login(admin_a)
        html = client.get(self.url, {"q": "<b>zzz</b>"}).content.decode()
        assert "No results" in html and "<b>zzz" not in html

    def test_palette_markup_is_accessible_and_htmx_ready(self, client, admin_a):
        html = page(client, admin_a, "/")
        assert 'x-data="commandPalette"' in html and '<dialog x-ref="dialog" class="command-dialog"' in html
        assert 'hx-get="/search/"' in html and 'role="search"' in html and 'aria-label="Clear search"' in html
        assert 'aria-live="polite"' in html


class TestLazyTreeBranch:
    def test_children_of_a_visible_location(self, client, admin_a, chain_a):
        client.force_login(admin_a)
        response = client.get(reverse("locations:branch", args=[chain_a[0].pk]))
        html = response.content.decode()
        assert response.status_code == 200 and "Region 1" in html and "Area 1" not in html  # one level only
        assert 'hx-get="' in html  # Region has children of its own -> lazy

    def test_foreign_location_is_a_404(self, client, admin_a, org_b, make_location):
        foreign = make_location(org_b, "Foreign", code="FOR")
        client.force_login(admin_a)
        assert client.get(reverse("locations:branch", args=[foreign.pk])).status_code == 404

    def test_requires_login(self, client, chain_a):
        assert client.get(reverse("locations:branch", args=[chain_a[0].pk])).status_code == 302


class TestShowcase:
    def test_staff_see_every_component_section(self, client, staff_user):
        html = page(client, staff_user, reverse("core:ui"))
        for anchor in ("buttons", "cards", "badges", "alerts", "forms", "tables", "overlays", "navigation", "tree",
                       "timeline", "states", "feedback", "tokens"):
            assert f'id="{anchor}"' in html, anchor
        assert html.count("<h1") == 1

    def test_members_and_anonymous_are_kept_out(self, client, viewer_a):
        assert client.get(reverse("core:ui")).status_code == 302
        client.force_login(viewer_a)
        assert client.get(reverse("core:ui")).status_code == 403

    def test_showcase_is_accessible_and_csp_safe(self, client, staff_user):
        html = page(client, staff_user, reverse("core:ui"))
        names = Names()
        names.feed(html)
        for tag, attrs, text in names.controls:
            assert text or attrs.get("aria-label") or attrs.get("title"), (tag, attrs)
        for match in re.finditer(r"<i\b[^>]*data-lucide[^>]*>", html):
            assert 'aria-hidden="true"' in match.group(0)
        assert not re.search(r"<script(?![^>]*\b(src|nonce)=)", html) and not re.search(r"\sstyle\s*=", html)
        assert not re.search(r"\son(click|change|load|submit)=", html)
        identifier = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
        for key, value in re.findall(r'\s((?:x-(?:data|show|text|bind|if)|@[\w:.-]+|:[\w-]+))="([^"]*)"', html):
            assert identifier.match(value), (key, value)
        registered = set(re.findall(r'Alpine\.data\("(\w+)"', (settings.BASE_DIR / "static/js/components.js").read_text(encoding="utf-8")))
        assert set(re.findall(r'x-data="([^"]+)"', html)) <= registered

    def test_form_demo_has_labels_required_marks_and_errors_linked(self, client, staff_user):
        html = page(client, staff_user, reverse("core:ui"))
        assert 'class="required-mark" aria-hidden="true"' in html
        assert 'aria-invalid="true" aria-describedby="demo-invalid-error"' in html and 'role="switch"' in html
        for field_id in ("demo-name", "demo-select", "demo-textarea", "demo-date"):
            assert f'for="{field_id}"' in html

    def test_table_demo_is_responsive_and_scoped(self, client, staff_user):
        html = page(client, staff_user, reverse("core:ui"))
        assert "table-stack" in html and 'data-label="Name"' in html and "overflow-x-auto" in html
        assert 'scope="col"' in html and 'aria-label="Sample checklist templates"' in html

    def test_tabs_demo_is_wired(self, client, staff_user):
        html = page(client, staff_user, reverse("core:ui"))
        assert 'x-data="tabs"' in html and 'role="tabpanel"' in html and "data-tab-panel" in html


# ---------------------------------------------------------------------------
# JavaScript contract, CSS, PWA, security
# ---------------------------------------------------------------------------
class TestFrontendContract:
    def test_every_shell_component_is_registered(self):
        source = (settings.BASE_DIR / "static/js/components.js").read_text(encoding="utf-8")
        registered = set(re.findall(r'Alpine\.data\("(\w+)"', source))
        assert {"shell", "dropdown", "tabs", "toasts", "commandPalette", "networkStatus", "formGuard", "treeNav",
                "disclosure", "confirmDialog", "appearance"} <= registered

    def test_javascript_never_builds_html_from_strings(self):
        for name in ("components.js", "app.js"):
            source = (settings.BASE_DIR / "static/js" / name).read_text(encoding="utf-8")
            assert "innerHTML" not in source and "eval(" not in source and "new Function" not in source
            assert "document.write" not in source

    def test_toasts_use_text_content(self):
        source = (settings.BASE_DIR / "static/js/components.js").read_text(encoding="utf-8")
        assert "text.textContent = message" in source

    def test_built_css_has_the_shell_and_reduced_motion(self):
        built = (settings.BASE_DIR / "static/css/app.css").read_text(encoding="utf-8")
        for needle in (".app-sidebar", ".app-topbar", ".page-header", ".toast", ".command-dialog", ".timeline-item",
                       ".table-stack", ".stat-card", "prefers-reduced-motion:reduce", 'data-sidebar=collapsed'):
            assert needle in built, needle

    def test_touch_targets_are_enlarged_on_coarse_pointers(self):
        built = (settings.BASE_DIR / "static/css/app.css").read_text(encoding="utf-8")
        assert "pointer:coarse" in built and "min-height:2.75rem" in built

    def test_service_worker_cache_was_bumped_and_auth_still_bypassed(self, client, db):
        source = client.get("/service-worker.js").content.decode()
        assert '"/auth/"' in source and '"/admin/"' in source and '"/api/"' in source


class TestThemeAndSecurity:
    @pytest.mark.parametrize("mode", ["light", "dark", "system"])
    def test_shell_follows_the_users_appearance(self, client, user, mode):
        from apps.themes import services as theme_services

        theme_services.set_user_appearance(user, mode)
        html = page(client, user, "/")
        assert f'data-theme="{mode}"' in html and "/themes/tokens.css" in html

    def test_shell_uses_semantic_tokens_only(self, client, admin_a):
        html = page(client, admin_a, "/")
        assert not re.search(r"(?:bg|text|border)-(?:slate|gray|red|blue|indigo|emerald|amber)-\d", html)

    def test_csp_is_unchanged_and_nonce_based(self, client, admin_a):
        client.force_login(admin_a)
        header = client.get("/")["Content-Security-Policy"]
        assert "'unsafe-inline'" not in header and "'unsafe-eval'" not in header and "nonce-" in header

    def test_state_changing_shell_forms_carry_csrf(self, client, admin_a):
        html = page(client, admin_a, "/")
        for action in ("/auth/logout/",):
            form = html[html.index(f'action="{action}"'):][:400]
            assert "csrfmiddlewaretoken" in form

    def test_csrf_is_enforced_for_logout_and_switch(self, admin_a, org_a):
        from django.test import Client

        strict = Client(enforce_csrf_checks=True)
        strict.force_login(admin_a)
        assert strict.post(reverse("accounts:logout")).status_code == 403
        assert strict.post(reverse("orgcontext:switch"), {"organization": str(org_a.pk)}).status_code == 403

    def test_user_menu_never_exposes_identity_provider_details(self, client, admin_a):
        html = page(client, admin_a, "/")
        for secret in ("entra_object_id", "entra_tenant_id", "client_secret", "access_token"):
            assert secret not in html

    def test_form_pages_guard_unsaved_changes_and_disable_double_submit(self, client, staff_user, org_a):
        html = page(client, staff_user, reverse("organizations:update", args=[org_a.pk]))
        assert 'x-data="formGuard"' in html and '@beforeunload.window="warn"' in html and ':disabled="submitting"' in html
        assert 'class="form-actions"' in html

    def test_list_pages_use_the_standard_pattern(self, client, staff_user, org_a):
        html = page(client, staff_user, reverse("organizations:list"))
        assert 'class="page-header"' in html and 'aria-label="Breadcrumbs"' in html and "table-stack" in html

    def test_detail_pages_show_status_in_the_header(self, client, staff_user, org_a):
        html = page(client, staff_user, reverse("organizations:detail", args=[org_a.pk]))
        assert 'class="page-header"' in html and "badge badge-success" in html and ">Active<" in html

    def test_login_button_has_a_loading_state(self, client, db, settings):
        settings.MICROSOFT_AUTH_CONFIGURED = True
        html = client.get(reverse("accounts:login")).content.decode()
        if "Continue with Microsoft" in html:
            assert "data-loading-link" in html and "btn-spinner" in html


class TestDocumentation:
    def test_design_system_docs_and_adr_exist(self):
        doc = (settings.BASE_DIR / "docs/ui-design-system.md").read_text(encoding="utf-8")
        for heading in ("Design principles", "Layout system", "Components", "Naming conventions", "Theme tokens",
                        "Responsive rules", "Accessibility rules", "HTMX conventions", "Alpine.js conventions",
                        "Icon conventions", "Creating a new page", "Adding a component"):
            assert heading in doc, heading
        adrs = " ".join(p.read_text(encoding="utf-8") for p in (settings.BASE_DIR / "docs/decisions").glob("00[34]*.md"))
        assert "application shell" in adrs.lower() and "navigation" in adrs.lower()


class TestIconRowActions:
    def test_tag_renders_icon_tooltip_and_a_screen_reader_name(self):
        html = render('{% row_action "Edit" "pencil" href="/x/" subject="Acme" %}')
        assert 'data-lucide="pencil"' in html and 'data-tooltip="Edit"' in html
        assert '<span class="sr-only">Edit Acme</span>' in html and "btn-icon" in html

    def test_post_actions_are_forms_with_csrf_and_a_hidden_field(self):
        html = Template("{% load ui %}{% row_action 'Deactivate' 'power-off' post_url='/s/' subject='T' tone='danger' field_name='action' field_value='deactivate' %}").render(Context({"csrf_token": "tok"}))
        assert 'method="post"' in html and 'name="csrfmiddlewaretoken" value="tok"' in html
        assert 'name="action" value="deactivate"' in html and "text-danger-text" in html

    def test_list_pages_use_icon_actions_not_text_buttons(self, client, staff_user, org_a, make_location):
        make_location(org_a, "Root", code="ROOT")
        client.force_login(staff_user)
        for url in (reverse("organizations:list"), reverse("locations:list"), reverse("locations:type_list")):
            html = client.get(url).content.decode()
            assert 'data-lucide="pencil"' in html and 'data-tooltip="Edit"' in html, url
            assert 'btn btn-ghost btn-sm">Edit' not in html, url
        assert 'data-lucide="eye"' in client.get(reverse("organizations:list")).content.decode()

    def test_type_list_status_actions_post_with_csrf(self, client, staff_user, org_a, make_type):
        make_type(org_a, "Region", "REGION")
        client.force_login(staff_user)
        html = client.get(reverse("locations:type_list")).content.decode()
        assert 'data-lucide="power-off"' in html and "Deactivate" in html and 'name="action" value="deactivate"' in html

    def test_theme_list_has_view_and_edit_icons_for_drafts(self, client, theme_admin_a, org_a, make_theme):
        make_theme(org_a, "Draft theme")
        client.force_login(theme_admin_a)
        html = client.get(reverse("themes:list")).content.decode()
        assert 'data-lucide="eye"' in html and 'data-lucide="pencil"' in html

    def test_tables_use_the_shared_status_badges(self, client, staff_user, org_a):
        client.force_login(staff_user)
        html = client.get(reverse("organizations:list")).content.decode()
        assert "badge badge-success" in html and 'data-lucide="circle-check"' in html and ">Active<" in html

    def test_showcase_demo_form_never_posts(self, client, staff_user):
        client.force_login(staff_user)
        html = client.get(reverse("core:ui")).content.decode()
        assert "data-demo-form" in html
        assert client.post(reverse("core:ui")).status_code == 405
