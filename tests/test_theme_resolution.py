"""Which look does a request get?  Precedence, tenant isolation, appearance modes, performance."""

from __future__ import annotations

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory

from apps.organizations.models import OrganizationMembership
from apps.themes import services
from apps.themes.constants import APPEARANCE_COOKIE, PREVIEW_SESSION_KEY, ThemeStatus
from apps.themes.resolution import default_resolved, resolve_theme
from apps.themes.tokens import default_tokens

pytestmark = pytest.mark.django_db

PRIMARY_SYSTEM = "#111111"
PRIMARY_A = "#aa0000"
PRIMARY_B = "#0000aa"


def request_for(user=None, cookies=None, session=None):
    request = RequestFactory().get("/")
    request.user = user or AnonymousUser()
    request.session = session if session is not None else {}
    request.COOKIES.update(cookies or {})
    return request


@pytest.fixture
def themed(org_a, org_b, make_theme):
    """System theme + a different theme per organization."""
    system = make_theme(None, "System", status=ThemeStatus.ACTIVE, tokens={"light": {"primary": PRIMARY_SYSTEM, "accent": "#00aa00"}})
    theme_a = make_theme(org_a, "Theme A", status=ThemeStatus.ACTIVE, tokens={"light": {"primary": PRIMARY_A}})
    theme_b = make_theme(org_b, "Theme B", status=ThemeStatus.ACTIVE, tokens={"light": {"primary": PRIMARY_B}})
    return system, theme_a, theme_b


class TestPrecedence:
    def test_built_in_defaults_when_nothing_is_configured(self, db):
        resolved = resolve_theme(request_for())
        assert resolved.source == "default" and resolved.theme is None
        assert resolved.tokens == default_tokens()
        assert resolved.mode == "system"

    def test_system_theme_applies_to_anonymous_visitors(self, themed):
        resolved = resolve_theme(request_for())
        assert resolved.source == "system"
        assert resolved.tokens["light"]["primary"] == PRIMARY_SYSTEM

    def test_organization_theme_overrides_the_system_theme(self, themed, user, org_a):
        OrganizationMembership.objects.create(user=user, organization=org_a)
        resolved = resolve_theme(request_for(user))
        assert resolved.source == "organization"
        assert resolved.tokens["light"]["primary"] == PRIMARY_A

    def test_unset_tokens_fall_through_to_the_system_theme(self, themed, user, org_a):
        OrganizationMembership.objects.create(user=user, organization=org_a)
        resolved = resolve_theme(request_for(user))
        assert resolved.tokens["light"]["accent"] == "#00aa00"  # only the system theme sets it
        assert resolved.tokens["light"]["background"] == default_tokens()["light"]["background"]

    def test_users_never_get_another_organizations_theme(self, themed, make_user, org_a, org_b):
        user_a = make_user(organization=org_a)
        user_b = make_user(organization=org_b)
        assert resolve_theme(request_for(user_a)).tokens["light"]["primary"] == PRIMARY_A
        assert resolve_theme(request_for(user_b)).tokens["light"]["primary"] == PRIMARY_B

    def test_user_without_an_organization_gets_the_system_theme(self, themed, user):
        assert resolve_theme(request_for(user)).source == "system"

    def test_staff_without_membership_get_the_system_theme(self, themed, make_user):
        staff = make_user(staff=True)
        assert resolve_theme(request_for(staff)).source == "system"

    def test_inactive_organization_falls_back_to_the_system_theme(self, themed, user, org_a):
        OrganizationMembership.objects.create(user=user, organization=org_a)
        org_a.status = "suspended"
        org_a.save()
        assert resolve_theme(request_for(user)).source == "system"

    def test_inactive_membership_is_ignored(self, themed, make_user, org_a):
        assert resolve_theme(request_for(make_user(organization=org_a, member_active=False))).source == "system"

    def test_default_membership_wins_for_multi_organization_users(self, themed, user, org_a, org_b):
        OrganizationMembership.objects.create(user=user, organization=org_a)
        OrganizationMembership.objects.create(user=user, organization=org_b, is_default=True)
        assert resolve_theme(request_for(user)).tokens["light"]["primary"] == PRIMARY_B

    def test_publishing_without_activating_changes_nothing(self, themed, org_a, make_theme, make_user):
        make_theme(org_a, "Candidate", status=ThemeStatus.PUBLISHED, tokens={"light": {"primary": "#123456"}})
        assert resolve_theme(request_for(make_user(organization=org_a))).tokens["light"]["primary"] == PRIMARY_A

    def test_activation_and_rollback_change_the_resolved_theme(self, org_a, make_theme, make_user):
        first = make_theme(org_a, "First", status=ThemeStatus.ACTIVE, tokens={"light": {"primary": "#111111"}})
        second = make_theme(org_a, "Second", status=ThemeStatus.PUBLISHED, tokens={"light": {"primary": "#222222"}})
        account = make_user(organization=org_a)
        assert resolve_theme(request_for(account)).tokens["light"]["primary"] == "#111111"
        second = services.activate_theme(second)
        assert resolve_theme(request_for(account)).tokens["light"]["primary"] == "#222222"
        services.rollback_theme(second)
        assert resolve_theme(request_for(account)).tokens["light"]["primary"] == "#111111"
        assert first.pk


class TestAppearanceMode:
    def test_default_is_system(self, db, user):
        assert resolve_theme(request_for(user)).mode == "system"

    @pytest.mark.parametrize("mode", ["light", "dark", "system"])
    def test_users_choice_is_used(self, user, mode):
        services.set_user_appearance(user, mode)
        assert resolve_theme(request_for(user)).mode == mode

    def test_user_dark_beats_an_organization_theme_that_prefers_light(self, org_a, make_theme, make_user):
        make_theme(org_a, "Light org", status=ThemeStatus.ACTIVE, default_appearance="light")
        account = make_user(organization=org_a)
        assert resolve_theme(request_for(account)).mode == "light"  # theme default
        services.set_user_appearance(account, "dark")
        assert resolve_theme(request_for(account)).mode == "dark"  # explicit choice wins

    def test_user_system_follows_the_browser_not_the_theme_default(self, org_a, make_theme, make_user):
        make_theme(org_a, "Dark org", status=ThemeStatus.ACTIVE, default_appearance="dark")
        account = make_user(organization=org_a)
        services.set_user_appearance(account, "system")
        assert resolve_theme(request_for(account)).mode == "system"

    def test_clearing_the_preference_returns_to_the_theme_default(self, org_a, make_theme, make_user):
        make_theme(org_a, "Dark org", status=ThemeStatus.ACTIVE, default_appearance="dark")
        account = make_user(organization=org_a)
        services.set_user_appearance(account, "light")
        services.set_user_appearance(account, None)
        assert resolve_theme(request_for(account)).mode == "dark"

    def test_two_users_of_one_organization_differ_without_touching_the_theme(self, org_a, make_theme, make_user):
        theme = make_theme(org_a, "Shared", status=ThemeStatus.ACTIVE)
        dark_user, light_user = make_user(organization=org_a), make_user(organization=org_a)
        services.set_user_appearance(dark_user, "dark")
        services.set_user_appearance(light_user, "light")
        a, b = resolve_theme(request_for(dark_user)), resolve_theme(request_for(light_user))
        assert (a.mode, b.mode) == ("dark", "light")
        assert a.tokens == b.tokens and a.version == b.version
        theme.refresh_from_db()
        assert theme.status == ThemeStatus.ACTIVE

    def test_anonymous_cookie_is_used(self, db):
        assert resolve_theme(request_for(cookies={APPEARANCE_COOKIE: "dark"})).mode == "dark"

    def test_garbage_cookie_is_ignored(self, db):
        assert resolve_theme(request_for(cookies={APPEARANCE_COOKIE: "<script>"})).mode == "system"

    def test_signed_in_users_ignore_the_cookie(self, user):
        services.set_user_appearance(user, "light")
        assert resolve_theme(request_for(user, cookies={APPEARANCE_COOKIE: "dark"})).mode == "light"

    def test_anonymous_falls_back_to_the_system_themes_default(self, make_theme):
        make_theme(None, "Dark system", status=ThemeStatus.ACTIVE, default_appearance="dark")
        assert resolve_theme(request_for()).mode == "dark"


class TestMetadata:
    def test_color_scheme_and_theme_colors(self, db, user):
        services.set_user_appearance(user, "dark")
        resolved = resolve_theme(request_for(user))
        assert resolved.color_scheme == "dark"
        assert resolved.theme_color == resolved.theme_color_dark
        services.set_user_appearance(user, "system")
        system = resolve_theme(request_for(user))
        assert system.color_scheme == "light dark" and system.theme_color == ""

    def test_pwa_colors_come_from_tokens(self, db):
        resolved = resolve_theme(request_for())
        tokens = default_tokens()
        assert resolved.pwa("light") == {
            "theme_color": tokens["light"]["sidebar_background"],
            "background_color": tokens["light"]["background"],
        }

    def test_version_changes_when_the_active_theme_changes(self, org_a, make_theme, make_user):
        account = make_user(organization=org_a)
        before = resolve_theme(request_for(account)).version
        make_theme(org_a, "New", status=ThemeStatus.ACTIVE)
        assert resolve_theme(request_for(account)).version != before

    def test_default_resolved_needs_no_database(self):
        resolved = default_resolved()
        assert resolved.source == "default" and resolved.tokens == default_tokens()


class TestPreview:
    def test_preview_is_the_top_layer_and_not_persisted(self, themed, org_a, make_theme, theme_admin_a):
        candidate = make_theme(org_a, "Candidate", tokens={"light": {"primary": "#123456"}})
        session = {PREVIEW_SESSION_KEY: str(candidate.pk)}
        resolved = resolve_theme(request_for(theme_admin_a, session=session))
        assert resolved.is_preview and resolved.source == "preview"
        assert resolved.tokens["light"]["primary"] == "#123456"
        assert resolved.tokens["light"]["accent"] == "#00aa00"  # system layer still underneath
        other = resolve_theme(request_for(theme_admin_a))
        assert not other.is_preview and other.tokens["light"]["primary"] == PRIMARY_A

    def test_preview_of_a_foreign_theme_is_ignored(self, themed, org_b, make_theme, theme_admin_a):
        foreign = make_theme(org_b, "Foreign", tokens={"light": {"primary": "#123456"}})
        session = {PREVIEW_SESSION_KEY: str(foreign.pk)}
        resolved = resolve_theme(request_for(theme_admin_a, session=session))
        assert not resolved.is_preview
        assert PREVIEW_SESSION_KEY not in session  # cleaned up

    def test_users_without_permission_cannot_preview(self, org_a, make_theme, make_user):
        theme = make_theme(org_a, "Published", status=ThemeStatus.PUBLISHED)
        member = make_user(organization=org_a)
        session = {PREVIEW_SESSION_KEY: str(theme.pk)}
        assert not resolve_theme(request_for(member, session=session)).is_preview

    def test_garbage_in_the_session_never_breaks_the_page(self, db, user):
        resolved = resolve_theme(request_for(user, session={PREVIEW_SESSION_KEY: "not-a-uuid"}))
        assert not resolved.is_preview


class TestPerformance:
    def test_resolution_is_memoised_per_request(self, themed, user, org_a, django_assert_num_queries):
        OrganizationMembership.objects.create(user=user, organization=org_a)
        request = request_for(user)
        resolve_theme(request)
        with django_assert_num_queries(0):
            resolve_theme(request)

    def test_authenticated_resolution_is_three_queries(self, themed, user, org_a, django_assert_max_num_queries):
        OrganizationMembership.objects.create(user=user, organization=org_a)
        with django_assert_max_num_queries(3):  # membership, active themes, appearance
            resolve_theme(request_for(user))

    def test_anonymous_resolution_is_one_query(self, themed, django_assert_num_queries):
        with django_assert_num_queries(1):
            resolve_theme(request_for())
