"""Phase 2: identity, user lifecycle, organization context and Entra sign-in.

The Microsoft provider is always mocked at the MSAL boundary
(``apps.accounts.entra.build_client``); no test needs a real Microsoft account,
and the suite proves the application never depends on Microsoft tokens.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import authenticate, get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts import entra, throttling
from apps.accounts import services
from apps.accounts.constants import (
    ACTIVE_ORGANIZATION_SESSION_KEY,
    OIDC_FLOW_SESSION_KEY,
    POST_LOGIN_REDIRECT_SESSION_KEY,
    UserStatus,
)
from apps.accounts.entra import EntraConfig, ExternalIdentity, claims_to_identity, tenant_is_allowed
from apps.accounts.exceptions import IdentityNotAllowed
from apps.accounts.selectors import (
    get_user_by_email,
    get_user_by_entra_identity,
    resolve_active_organization,
    users_visible_to,
)
from apps.core.models import ActivityLog
from apps.organizations.models import OrganizationMembership

User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_auth_throttle():
    """Keep the throttle counters isolated between tests (LocMem cache persists)."""
    cache.clear()
    yield
    cache.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def identity(**overrides) -> ExternalIdentity:
    data = {
        "object_id": "oid-1",
        "tenant_id": "tenant-1",
        "email": "microsoft.user@example.com",
        "username": "microsoft.user@example.com",
        "display_name": "Microsoft User",
        "first_name": "Microsoft",
        "last_name": "User",
        "job_title": "Auditor",
    }
    data.update(overrides)
    return ExternalIdentity(**data)


def config(**overrides) -> EntraConfig:
    data = {
        "client_id": "client",
        "client_secret": "secret",
        "tenant_id": "tenant-1",
        "authority": "https://login.microsoftonline.com/tenant-1",
        "redirect_uri": "http://testserver/auth/microsoft/callback/",
        "post_logout_redirect_uri": "",
        "scopes": ("openid", "profile", "email"),
        "allowed_tenants": (),
    }
    data.update(overrides)
    return EntraConfig(**data)


class FakeMsalClient:
    def __init__(self, *, result=None, auth_uri="https://login.microsoftonline.com/x", raises=None):
        self.result = result
        self.auth_uri = auth_uri
        self.raises = raises
        self.initiated = None

    def initiate_auth_code_flow(self, scopes, redirect_uri):
        self.initiated = {"scopes": scopes, "redirect_uri": redirect_uri}
        return {"auth_uri": self.auth_uri, "state": "state", "nonce": "nonce", "code_verifier": "cv"}

    def acquire_token_by_auth_code_flow(self, flow, params):
        if self.raises is not None:
            raise self.raises
        return self.result


@pytest.fixture
def entra_settings(settings):
    settings.MICROSOFT_CLIENT_ID = "client"
    settings.MICROSOFT_CLIENT_SECRET = "secret"
    settings.MICROSOFT_TENANT_ID = "tenant-1"
    settings.MICROSOFT_AUTHORITY = "https://login.microsoftonline.com/tenant-1"
    settings.MICROSOFT_REDIRECT_URI = "http://testserver/auth/microsoft/callback/"
    settings.MICROSOFT_END_SESSION_ENDPOINT = "https://login.microsoftonline.com/tenant-1/oauth2/v2.0/logout"
    settings.MICROSOFT_AUTH_CONFIGURED = True
    settings.MICROSOFT_SCOPES = ["openid", "profile", "email"]
    return settings


# ---------------------------------------------------------------------------
# Model / lifecycle invariant
# ---------------------------------------------------------------------------
class TestUserLifecycle:
    def test_default_user_is_active(self, user):
        assert user.status == UserStatus.ACTIVE
        assert user.is_active is True
        assert user.can_sign_in is True

    def test_create_user_is_active_false_translates_to_inactive(self):
        account = User.objects.create_user(email="gone@example.com", password="x", is_active=False)
        assert account.status == UserStatus.INACTIVE
        assert account.is_active is False

    def test_status_is_authoritative_and_mirror_follows_on_save(self, user):
        user.status = UserStatus.SUSPENDED
        user.save(update_fields=["status"])
        user.refresh_from_db()
        assert user.status == UserStatus.SUSPENDED
        assert user.is_active is False
        assert user.can_sign_in is False

    def test_database_rejects_contradictory_rows(self, user):
        # Bypass the model to write a row that disagrees with the invariant.
        with pytest.raises(IntegrityError):
            User.objects.filter(pk=user.pk).update(is_active=True, status=UserStatus.SUSPENDED)

    def test_pending_user_cannot_sign_in(self):
        account = User.objects.create_user(email="pending@example.com", status=UserStatus.PENDING)
        assert account.is_pending is True
        assert account.is_active is False
        assert account.can_sign_in is False


# ---------------------------------------------------------------------------
# External identity parsing / tenant policy
# ---------------------------------------------------------------------------
class TestEntraParsing:
    def test_claims_are_mapped_to_the_needed_subset(self):
        parsed = claims_to_identity(
            {
                "oid": "abc",
                "tid": "tenant-1",
                "preferred_username": "a@example.com",
                "name": "Alice",
                "given_name": "Alice",
                "family_name": "Smith",
                "jobTitle": "Auditor",
                "access_token": "SHOULD-NOT-BE-RETAINED",
            }
        )
        assert parsed.object_id == "abc"
        assert parsed.tenant_id == "tenant-1"
        assert parsed.email == "a@example.com"
        assert parsed.first_name == "Alice"
        assert not hasattr(parsed, "access_token")

    def test_tenant_is_allowed_only_for_configured_tenant(self):
        assert tenant_is_allowed("tenant-1", config()) is True
        assert tenant_is_allowed("other", config()) is False

    def test_explicit_allow_list_enables_multi_tenant(self):
        cfg = config(allowed_tenants=("tenant-1", "tenant-2"))
        assert tenant_is_allowed("tenant-2", cfg) is True
        assert tenant_is_allowed("tenant-3", cfg) is False


# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------
class TestSelectors:
    def test_lookup_by_entra_identity(self, user):
        user.entra_object_id = "oid-x"
        user.save()
        assert get_user_by_entra_identity("oid-x").pk == user.pk
        assert get_user_by_entra_identity("missing") is None

    def test_lookup_by_email_is_case_insensitive(self, user):
        assert get_user_by_email("AUDITOR@EXAMPLE.COM").pk == user.pk

    def test_users_visible_to_is_tenant_scoped(self, admin_a, admin_b, viewer_a):
        visible = set(users_visible_to(admin_a).values_list("pk", flat=True))
        assert admin_a.pk in visible
        assert viewer_a.pk in visible
        assert admin_b.pk not in visible

    def test_platform_staff_see_everyone(self, staff_user, admin_b):
        assert admin_b.pk in set(users_visible_to(staff_user).values_list("pk", flat=True))

    def test_inactive_user_sees_no_one(self, user, admin_a):
        user.status = UserStatus.INACTIVE
        user.save(update_fields=["status"])
        assert not users_visible_to(user).exists()

    def test_resolve_active_organization_honours_a_valid_id(self, user, org_a, org_b):
        OrganizationMembership.objects.create(user=user, organization=org_a)
        OrganizationMembership.objects.create(user=user, organization=org_b)
        assert resolve_active_organization(user, str(org_b.pk)).pk == org_b.pk

    def test_resolve_active_organization_ignores_a_foreign_id(self, user, org_a, org_b, admin_b):
        OrganizationMembership.objects.create(user=user, organization=org_a)
        # org_b is not visible to `user`; must fall back, never trust the id.
        assert resolve_active_organization(user, str(org_b.pk)).pk == org_a.pk

    def test_resolve_active_organization_garbage_id_falls_back(self, user, org_a):
        OrganizationMembership.objects.create(user=user, organization=org_a)
        assert resolve_active_organization(user, "not-a-uuid").pk == org_a.pk

    def test_resolve_active_organization_prefers_default_membership(self, user, org_a, org_b):
        OrganizationMembership.objects.create(user=user, organization=org_a)
        OrganizationMembership.objects.create(user=user, organization=org_b, is_default=True)
        assert resolve_active_organization(user, None).pk == org_b.pk

    def test_resolve_active_organization_returns_none_without_orgs(self, user):
        assert resolve_active_organization(user, None) is None


# ---------------------------------------------------------------------------
# Services: provisioning / linking / synchronisation
# ---------------------------------------------------------------------------
class TestProvisioning:
    def test_provision_defaults_to_pending(self, settings):
        account = services.provision_user(identity())
        assert account.status == UserStatus.PENDING
        assert account.is_active is False
        assert account.entra_object_id == "oid-1"
        assert account.entra_tenant_id == "tenant-1"
        assert not account.has_usable_password()
        assert ActivityLog.objects.filter(verb="user.created", target_id=str(account.pk)).exists()

    def test_provision_can_be_active_when_configured(self, settings):
        settings.AUTH_PROVISIONING_MODE = "active"
        account = services.provision_user(identity(email="auto@example.com"))
        assert account.status == UserStatus.ACTIVE

    def test_link_non_staff_account_by_email(self, user):
        linked = services.link_external_identity(user, identity(email=user.email))
        assert linked.entra_object_id == "oid-1"
        assert ActivityLog.objects.filter(verb="identity.linked").exists()

    def test_link_refuses_a_mismatched_object_id(self, user):
        user.entra_object_id = "different"
        user.save()
        with pytest.raises(IdentityNotAllowed) as exc:
            services.link_external_identity(user, identity())
        assert exc.value.reason == "identity_conflict"

    def test_sync_updates_idp_fields_and_never_status(self, user):
        user.entra_object_id = "oid-1"
        user.status = UserStatus.SUSPENDED
        user.save()
        services.sync_user_identity(
            user, identity(display_name="New Name", job_title="Lead Auditor")
        )
        user.refresh_from_db()
        assert user.display_name == "New Name"
        assert user.job_title == "Lead Auditor"
        assert user.status == UserStatus.SUSPENDED  # untouched

    def test_sync_does_not_duplicate_an_email_taken_by_another_user(self, user, admin_a):
        user.entra_object_id = "oid-1"
        user.save()
        services.sync_user_identity(user, identity(email=admin_a.email))
        user.refresh_from_db()
        assert user.email != admin_a.email


class TestResolveEntraUser:
    @pytest.fixture(autouse=True)
    def _entra(self, entra_settings):
        return entra_settings

    def test_rejects_disallowed_tenant(self):
        with pytest.raises(IdentityNotAllowed) as exc:
            services.resolve_entra_user(identity(tenant_id="other"))
        assert exc.value.reason == "tenant_not_allowed"
        assert not User.objects.filter(entra_object_id="oid-1").exists()

    def test_provisions_pending_and_refuses_first_login(self):
        with pytest.raises(IdentityNotAllowed) as exc:
            services.resolve_entra_user(identity())
        assert exc.value.reason == "account_pending"
        # The account was still created for an administrator to approve.
        assert User.objects.filter(entra_object_id="oid-1").exists()

    def test_active_linked_user_signs_in_and_is_synced(self):
        account = services.provision_user(identity())
        services.set_user_status(account, UserStatus.ACTIVE)
        resolved, created = services.resolve_entra_user(identity(display_name="Updated"))
        assert resolved.pk == account.pk
        assert created is False
        resolved.refresh_from_db()
        assert resolved.display_name == "Updated"

    def test_email_conflict_with_a_privileged_account_is_refused(self, superuser):
        # An existing staff/superuser account is never linked by email.
        superuser.email = "microsoft.user@example.com"
        superuser.save()
        with pytest.raises(IdentityNotAllowed) as exc:
            services.resolve_entra_user(identity(email=superuser.email))
        assert exc.value.reason == "identity_conflict"

    def test_email_linking_disabled_refuses_conflict(self, settings, user):
        settings.AUTH_EMAIL_LINKING = "disabled"
        with pytest.raises(IdentityNotAllowed) as exc:
            services.resolve_entra_user(identity(email=user.email))
        assert exc.value.reason == "identity_conflict"

    def test_missing_email_is_refused(self):
        with pytest.raises(IdentityNotAllowed) as exc:
            services.resolve_entra_user(identity(email="", username=""))
        assert exc.value.reason == "missing_email"


class TestLifecycleServices:
    def test_set_user_status_logs_and_mirrors(self, user, superuser):
        services.set_user_status(user, UserStatus.SUSPENDED, actor=superuser)
        user.refresh_from_db()
        assert user.status == UserStatus.SUSPENDED
        assert user.is_active is False
        assert ActivityLog.objects.filter(verb="user.suspended").exists()

    def test_set_user_status_rejects_unknown_status(self, user):
        with pytest.raises(ValidationError):
            services.set_user_status(user, "not-a-status")

    def test_activate_after_suspend(self, user):
        services.suspend_user(user)
        services.activate_user(user)
        user.refresh_from_db()
        assert user.status == UserStatus.ACTIVE
        assert user.is_active is True


class TestMembershipServices:
    def test_add_membership_is_idempotent_and_logs(self, user, org_a, superuser):
        services.add_membership(user, org_a, actor=superuser)
        services.add_membership(user, org_a, actor=superuser)
        assert OrganizationMembership.objects.filter(user=user, organization=org_a).count() == 1
        assert ActivityLog.objects.filter(verb="organization_membership.created").count() == 1

    def test_set_default_organization_clears_others(self, user, org_a, org_b):
        services.add_membership(user, org_a, is_default=True)
        services.set_default_organization(user, org_b)
        defaults = set(
            OrganizationMembership.objects.filter(user=user, is_default=True).values_list(
                "organization_id", flat=True
            )
        )
        assert defaults == {org_b.pk}

    def test_remove_membership(self, user, org_a):
        membership = services.add_membership(user, org_a)
        left = services.remove_membership(membership)
        row = OrganizationMembership.objects.get(user=user, organization=org_a)  # kept, not deleted
        assert (row.status, row.is_active, row.is_default) == ("left", False, False)
        assert row.left_at is not None and left.pk == row.pk


# ---------------------------------------------------------------------------
# Authentication backend
# ---------------------------------------------------------------------------
class TestBackend:
    @pytest.fixture(autouse=True)
    def _entra(self, entra_settings):
        return entra_settings

    def test_backend_is_silent_without_our_credential(self, rf):
        assert authenticate(rf.get("/"), username="x", password="y") is None

    def test_backend_resolves_a_verified_identity(self):
        account = services.provision_user(identity())
        services.set_user_status(account, UserStatus.ACTIVE)
        resolved = authenticate(rf_get(), msal_identity=identity())
        assert resolved.pk == account.pk
        assert resolved.backend == "apps.accounts.backends.EntraOIDCBackend"

    def test_backend_raises_for_a_pending_identity(self):
        with pytest.raises(IdentityNotAllowed):
            authenticate(rf_get(), msal_identity=identity())


def rf_get():
    from django.test import RequestFactory

    return RequestFactory().get("/")


# ---------------------------------------------------------------------------
# Sign-in views (MSAL mocked at the boundary)
# ---------------------------------------------------------------------------
class TestSignInViews:
    def test_login_page_offers_microsoft_first(self, client, entra_settings):
        response = client.get(reverse("accounts:login"))
        body = response.content.decode()
        assert response.status_code == 200
        assert reverse("accounts:microsoft_login") in body
        assert body.index("Continue with Microsoft") < body.index("local password")

    def test_microsoft_login_redirects_to_the_authorization_endpoint(self, client, entra_settings, monkeypatch):
        fake = FakeMsalClient()
        monkeypatch.setattr(entra, "build_client", lambda: fake)
        response = client.get(reverse("accounts:microsoft_login"))
        assert response.status_code == 302
        assert response["Location"] == fake.auth_uri
        assert OIDC_FLOW_SESSION_KEY in client.session
        assert "openid" in fake.initiated["scopes"]

    def test_microsoft_login_stores_only_safe_next(self, client, entra_settings, monkeypatch):
        monkeypatch.setattr(entra, "build_client", lambda: FakeMsalClient())
        client.get(reverse("accounts:microsoft_login") + "?next=https://evil.example.com")
        assert POST_LOGIN_REDIRECT_SESSION_KEY not in client.session

        client.get(reverse("accounts:microsoft_login") + "?next=" + reverse("accounts:profile"))
        assert client.session[POST_LOGIN_REDIRECT_SESSION_KEY] == reverse("accounts:profile")

    def test_microsoft_login_without_configuration_redirects_to_login(self, client):
        response = client.get(reverse("accounts:microsoft_login"))
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:login")

    def test_callback_success_signs_the_user_in(self, client, entra_settings, monkeypatch):
        account = services.provision_user(identity())
        services.set_user_status(account, UserStatus.ACTIVE)
        result = {
            "id_token_claims": {
                "oid": "oid-1",
                "tid": "tenant-1",
                "preferred_username": account.email,
                "name": "Microsoft User",
            },
            "access_token": "SECRET-TOKEN",
            "refresh_token": "SECRET-REFRESH",
        }
        monkeypatch.setattr(entra, "build_client", lambda: FakeMsalClient(result=result))

        client.get(reverse("accounts:microsoft_login"))
        response = client.get(reverse("accounts:microsoft_callback"))

        assert response.status_code == 302
        assert response["Location"] == reverse("core:home")
        assert client.session["_auth_user_id"] == str(account.pk)
        # Tokens are never persisted.
        assert "SECRET-TOKEN" not in (client.session.get("access_token") or "")
        assert OIDC_FLOW_SESSION_KEY not in client.session

    def test_callback_for_pending_user_shows_a_safe_error(self, client, entra_settings, monkeypatch):
        result = {
            "id_token_claims": {
                "oid": "oid-1",
                "tid": "tenant-1",
                "preferred_username": "new@example.com",
                "name": "New Person",
            }
        }
        monkeypatch.setattr(entra, "build_client", lambda: FakeMsalClient(result=result))
        client.get(reverse("accounts:microsoft_login"))
        response = client.get(reverse("accounts:microsoft_callback"), follow=True)
        body = response.content.decode()
        assert "awaiting administrator approval" in body
        assert client.session.get("_auth_user_id") is None

    def test_callback_without_a_flow_is_rejected(self, client, entra_settings):
        response = client.get(reverse("accounts:microsoft_callback"))
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:auth_error")

    def test_callback_provider_error_does_not_leak_details(self, client, entra_settings, monkeypatch):
        result = {"error": "access_denied", "error_description": "AADSTS65004: raw detail"}
        monkeypatch.setattr(entra, "build_client", lambda: FakeMsalClient(result=result))
        client.get(reverse("accounts:microsoft_login"))
        response = client.get(reverse("accounts:microsoft_callback"), follow=True)
        body = response.content.decode()
        assert "AADSTS65004" not in body
        assert "raw detail" not in body

    def test_error_page_maps_known_reasons(self, client):
        session = client.session
        session["auth_error_reason"] = "account_suspended"
        session.save()
        response = client.get(reverse("accounts:auth_error"))
        assert "suspended" in response.content.decode().lower()


# ---------------------------------------------------------------------------
# Middleware: lifecycle enforcement on every request
# ---------------------------------------------------------------------------
class TestIdentityMiddleware:
    def test_user_suspended_mid_session_is_signed_out(self, client, user):
        client.force_login(user)
        user.status = UserStatus.SUSPENDED
        user.save(update_fields=["status"])

        response = client.get(reverse("accounts:profile"))
        assert response.status_code == 302
        assert reverse("accounts:login") in response["Location"]
        assert client.session.get("_auth_user_id") is None


# ---------------------------------------------------------------------------
# Account & organization-context views
# ---------------------------------------------------------------------------
class TestAccountViews:
    def test_profile_requires_login(self, client):
        response = client.get(reverse("accounts:profile"))
        assert response.status_code == 302
        assert reverse("accounts:login") in response["Location"]

    def test_profile_renders_for_authenticated_user(self, client, user, membership):
        client.force_login(user)
        response = client.get(reverse("accounts:profile"))
        assert response.status_code == 200
        assert user.email in response.content.decode()

    def test_profile_edit_updates_application_managed_fields(self, client, user):
        client.force_login(user)
        response = client.post(
            reverse("accounts:profile_edit"), {"phone_number": "+44 20 7946 0000"}
        )
        assert response.status_code == 302
        user.refresh_from_db()
        assert user.phone_number == "+44 20 7946 0000"

    def test_preferences_renders_memberships(self, client, user, membership):
        client.force_login(user)
        response = client.get(reverse("accounts:preferences"))
        assert response.status_code == 200
        assert membership.organization.name in response.content.decode()

    def test_appearance_preference_is_stored_via_the_theme_engine(self, client, user):
        from apps.themes.models import UserAppearance

        client.force_login(user)
        response = client.post(reverse("accounts:appearance"), {"mode": "dark"})
        assert response.status_code == 302
        assert UserAppearance.objects.get(user=user).mode == "dark"


class TestOrganizationContext:
    def test_select_lists_only_visible_organizations(self, client, user, org_a, org_b):
        OrganizationMembership.objects.create(user=user, organization=org_a)
        client.force_login(user)
        body = client.get(reverse("orgcontext:select")).content.decode()
        assert org_a.name in body
        assert org_b.name not in body

    def test_switch_sets_the_active_organization(self, client, user, org_a):
        OrganizationMembership.objects.create(user=user, organization=org_a)
        client.force_login(user)
        response = client.post(reverse("orgcontext:switch"), {"organization": str(org_a.pk)})
        assert response.status_code == 302
        assert client.session[ACTIVE_ORGANIZATION_SESSION_KEY] == str(org_a.pk)

    def test_switch_to_an_invisible_organization_is_not_found(self, client, user, org_a, org_b):
        OrganizationMembership.objects.create(user=user, organization=org_a)
        client.force_login(user)
        response = client.post(reverse("orgcontext:switch"), {"organization": str(org_b.pk)})
        assert response.status_code == 404

    def test_switch_requires_post(self, client, user, org_a):
        OrganizationMembership.objects.create(user=user, organization=org_a)
        client.force_login(user)
        assert client.get(reverse("orgcontext:switch")).status_code == 405


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
class TestIdentityAPI:
    def test_me_requires_authentication(self, client):
        assert client.get("/api/v1/me/").status_code == 403

    def test_me_returns_the_caller(self, admin_a):
        client = APIClient()
        client.force_authenticate(admin_a)
        payload = client.get("/api/v1/me/").json()["data"]
        assert payload["email"] == admin_a.email
        assert payload["status"] == UserStatus.ACTIVE

    def test_me_patch_updates_phone_only(self, admin_a):
        client = APIClient()
        client.force_authenticate(admin_a)
        response = client.patch(
            "/api/v1/me/", {"phone_number": "+1 555 0100", "email": "hacked@example.com"}
        )
        assert response.status_code == 200
        admin_a.refresh_from_db()
        assert admin_a.phone_number == "+1 555 0100"
        assert admin_a.email != "hacked@example.com"

    def test_users_list_is_tenant_scoped(self, admin_a, admin_b, viewer_a):
        client = APIClient()
        client.force_authenticate(admin_a)
        emails = {row["email"] for row in client.get("/api/v1/users/").json()["data"]}
        assert admin_a.email in emails
        assert viewer_a.email in emails
        assert admin_b.email not in emails

    def test_users_search(self, admin_a, viewer_a):
        client = APIClient()
        client.force_authenticate(admin_a)
        response = client.get("/api/v1/users/", {"search": "viewer-a"})
        emails = {row["email"] for row in response.json()["data"]}
        assert emails == {viewer_a.email}


# ---------------------------------------------------------------------------
# URL map
# ---------------------------------------------------------------------------
class TestIdentityURLs:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("accounts:microsoft_login", "/auth/microsoft/login/"),
            ("accounts:microsoft_callback", "/auth/microsoft/callback/"),
            ("accounts:auth_error", "/auth/error/"),
            ("accounts:preferences", "/auth/preferences/"),
            ("accounts:appearance", "/auth/appearance/"),
            ("accounts:profile_edit", "/auth/profile/edit/"),
            ("orgcontext:select", "/organizations/select/"),
            ("orgcontext:switch", "/organizations/switch/"),
        ],
    )
    def test_reverse(self, name, expected):
        assert reverse(name) == expected


# ---------------------------------------------------------------------------
# Sign-in throttling
# ---------------------------------------------------------------------------
class TestAuthThrottling:
    def test_repeated_password_failures_are_blocked(self, client, user, settings):
        settings.AUTH_THROTTLE_MAX_ATTEMPTS = 3
        url = reverse("accounts:login")
        for _ in range(3):
            response = client.post(url, {"username": user.email, "password": "wrong"})
            assert response.status_code == 200  # the form is re-rendered
        assert client.post(url, {"username": user.email, "password": "wrong"}).status_code == 429

    def test_successful_login_clears_the_counter(self, client, user, settings):
        settings.AUTH_THROTTLE_MAX_ATTEMPTS = 2
        url = reverse("accounts:login")
        client.post(url, {"username": user.email, "password": "wrong"})
        signed_in = client.post(url, {"username": user.email, "password": "Str0ng-Passw0rd!"})
        assert signed_in.status_code == 302

        # The counter is cleared: after signing out, the limit starts over.
        client.logout()
        for _ in range(2):
            assert client.post(url, {"username": user.email, "password": "wrong"}).status_code == 200
        assert client.post(url, {"username": user.email, "password": "wrong"}).status_code == 429

    def test_throttled_response_is_safe(self, client, user, settings):
        settings.AUTH_THROTTLE_MAX_ATTEMPTS = 1
        url = reverse("accounts:login")
        client.post(url, {"username": user.email, "password": "wrong"})
        blocked = client.post(url, {"username": user.email, "password": "wrong"})
        body = blocked.content.decode()
        assert blocked.status_code == 429
        assert "Too many sign-in attempts" in body
        assert user.email not in body  # no account enumeration

    def test_get_is_never_blocked(self, client, user, settings):
        settings.AUTH_THROTTLE_MAX_ATTEMPTS = 1
        url = reverse("accounts:login")
        client.post(url, {"username": user.email, "password": "wrong"})
        assert client.get(url).status_code == 200

    def test_callback_failures_are_blocked(self, client, settings):
        settings.AUTH_THROTTLE_MAX_ATTEMPTS = 2
        url = reverse("accounts:microsoft_callback")
        for _ in range(2):
            assert client.get(url).status_code == 302  # redirected to the error page
        assert client.get(url).status_code == 429

    def test_is_blocked_and_record_failure_round_trip(self, rf, settings):
        settings.AUTH_THROTTLE_MAX_ATTEMPTS = 2
        request = rf.get("/")
        assert throttling.is_blocked(request, throttling.PASSWORD_SCOPE) is False
        throttling.record_failure(request, throttling.PASSWORD_SCOPE)
        assert throttling.is_blocked(request, throttling.PASSWORD_SCOPE) is False
        throttling.record_failure(request, throttling.PASSWORD_SCOPE)
        assert throttling.is_blocked(request, throttling.PASSWORD_SCOPE) is True
        throttling.clear(request, throttling.PASSWORD_SCOPE)
        assert throttling.is_blocked(request, throttling.PASSWORD_SCOPE) is False
