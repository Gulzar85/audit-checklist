"""Phase 2 identity foundation: membership lifecycle, scope foundation, preferences, unlink,
session lifetime / PWA behaviour, hardening, cross-organization security and theme integration."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts import services
from apps.accounts.constants import ACTIVE_ORGANIZATION_SESSION_KEY, UserStatus
from apps.accounts.exceptions import REASON_MESSAGES
from apps.accounts.middleware import AUTH_STARTED_KEY, LAST_ACTIVITY_KEY, SESSION_EXPIRED_CODE
from apps.accounts.models import User, UserPreference
from apps.accounts.preferences import PREFERENCES, validate_preference
from apps.accounts.throttling import client_address
from apps.core.models import ActivityLog
from apps.locations import scope
from apps.locations.models import MembershipLocationScope
from apps.organizations.models import Organization, OrganizationMembership
from apps.organizations.selectors import organizations_visible_to
from apps.themes import services as theme_services

pytestmark = pytest.mark.django_db
Status = OrganizationMembership.Status


def verbs(user=None):
    return list(ActivityLog.objects.order_by("created_at").values_list("verb", flat=True))


# ---------------------------------------------------------------------------
# Membership lifecycle
# ---------------------------------------------------------------------------
class TestMembershipLifecycle:
    def test_new_membership_is_active_with_a_consistent_mirror(self, user, org_a):
        membership = services.add_membership(user, org_a)
        assert (membership.status, membership.is_active, membership.left_at) == (Status.ACTIVE, True, None)

    def test_suspend_and_reinstate(self, user, org_a):
        membership = services.add_membership(user, org_a)
        suspended = services.set_membership_status(membership, Status.SUSPENDED)
        assert (suspended.status, suspended.is_active, suspended.left_at) == (Status.SUSPENDED, False, None)
        active = services.set_membership_status(suspended, Status.ACTIVE)
        assert (active.status, active.is_active) == (Status.ACTIVE, True)

    def test_leaving_keeps_the_row_and_stamps_left_at(self, user, org_a):
        membership = services.add_membership(user, org_a, is_default=True)
        left = services.remove_membership(membership)
        assert (left.status, left.is_active, left.is_default) == (Status.LEFT, False, False)
        assert left.left_at is not None
        assert OrganizationMembership.objects.filter(pk=membership.pk).exists()

    def test_rejoining_reuses_the_row_and_clears_left_at(self, user, org_a):
        first = services.add_membership(user, org_a)
        services.remove_membership(first)
        again = services.add_membership(user, org_a)
        assert again.pk == first.pk and (again.status, again.left_at) == (Status.ACTIVE, None)
        assert OrganizationMembership.objects.filter(user=user, organization=org_a).count() == 1
        assert ActivityLog.objects.filter(verb="organization_membership.created", metadata__rejoined=True).exists()

    def test_duplicate_membership_is_prevented(self, user, org_a):
        OrganizationMembership.objects.create(user=user, organization=org_a)
        with pytest.raises(IntegrityError), transaction.atomic():
            OrganizationMembership.objects.create(user=user, organization=org_a)

    def test_user_can_belong_to_several_organizations(self, user, org_a, org_b):
        services.add_membership(user, org_a)
        services.add_membership(user, org_b)
        assert set(organizations_visible_to(user)) == {org_a, org_b}

    @pytest.mark.parametrize("status", [Status.SUSPENDED, Status.LEFT])
    def test_non_active_membership_grants_no_access(self, user, org_a, status):
        membership = services.add_membership(user, org_a)
        services.set_membership_status(membership, status)
        assert list(organizations_visible_to(user)) == []

    def test_unknown_status_is_rejected(self, user, org_a):
        with pytest.raises(ValidationError):
            services.set_membership_status(services.add_membership(user, org_a), "banished")

    def test_a_legacy_is_active_false_write_means_suspended(self, user, org_a):
        membership = OrganizationMembership.objects.create(user=user, organization=org_a, is_active=False)
        assert (membership.status, membership.is_active) == (Status.SUSPENDED, False)

    def test_database_forbids_a_contradictory_row(self, user, org_a):
        membership = OrganizationMembership.objects.create(user=user, organization=org_a)
        with pytest.raises(IntegrityError), transaction.atomic():
            OrganizationMembership.objects.filter(pk=membership.pk).update(is_active=False)  # status stays active
        with pytest.raises(IntegrityError), transaction.atomic():
            OrganizationMembership.objects.filter(pk=membership.pk).update(status="left", is_active=False)  # no left_at

    def test_status_changes_are_audited(self, user, org_a, staff_user):
        membership = services.add_membership(user, org_a, actor=staff_user)
        services.set_membership_status(membership, Status.SUSPENDED, actor=staff_user)
        services.remove_membership(membership, actor=staff_user)
        assert verbs() == [
            "organization_membership.created",
            "organization_membership.suspended",
            "organization_membership.removed",
        ]
        entry = ActivityLog.objects.get(verb="organization_membership.suspended")
        assert entry.actor == staff_user and entry.organization == org_a
        assert entry.metadata["from"] == "active" and entry.metadata["to"] == "suspended"

    def test_history_records_status_changes(self, user, org_a):
        membership = services.add_membership(user, org_a)
        services.set_membership_status(membership, Status.SUSPENDED)
        assert list(membership.history.order_by("history_date").values_list("status", flat=True)) == ["active", "suspended"]

    def test_set_membership_active_wrapper(self, user, org_a):
        membership = services.add_membership(user, org_a)
        assert services.set_membership_active(membership, False).status == Status.SUSPENDED
        assert services.set_membership_active(membership, True).status == Status.ACTIVE


class TestMembershipAdmin:
    def test_inline_changes_go_through_the_services(self, client, superuser, user, org_a):
        membership = services.add_membership(user, org_a)
        client.force_login(superuser)
        data = {
            "name": org_a.name, "slug": org_a.slug,
            "memberships-TOTAL_FORMS": "1", "memberships-INITIAL_FORMS": "1",
            "memberships-MIN_NUM_FORMS": "0", "memberships-MAX_NUM_FORMS": "1000",
            "memberships-0-id": str(membership.pk), "memberships-0-organization": str(org_a.pk),
            "memberships-0-user": str(user.pk), "memberships-0-status": "suspended", "_save": "Save",
        }
        assert client.post(reverse("admin:organizations_organization_change", args=[org_a.pk]), data).status_code == 302
        membership.refresh_from_db()
        assert membership.status == Status.SUSPENDED
        assert ActivityLog.objects.filter(verb="organization_membership.suspended", actor=superuser).exists()

    def test_standalone_actions(self, client, superuser, user, org_a):
        membership = services.add_membership(user, org_a)
        client.force_login(superuser)
        url = reverse("admin:organizations_organizationmembership_changelist")
        for action, expected in (("suspend_selected", "suspended"), ("reinstate_selected", "active"), ("leave_selected", "left")):
            client.post(url, {"action": action, "_selected_action": [str(membership.pk)]}, follow=True)
            membership.refresh_from_db()
            assert membership.status == expected
        assert client.get(reverse("admin:organizations_organizationmembership_delete", args=[membership.pk])).status_code == 403

    def test_user_admin_status_actions_work(self, client, superuser, user):
        """Regression: ``_apply_status`` used ``self`` inside a staticmethod."""
        client.force_login(superuser)
        url = reverse("admin:accounts_user_changelist")
        for action, expected in (("suspend_users", UserStatus.SUSPENDED), ("approve_users", UserStatus.ACTIVE), ("deactivate_users", UserStatus.INACTIVE)):
            response = client.post(url, {"action": action, "_selected_action": [str(user.pk)]}, follow=True)
            assert response.status_code == 200
            user.refresh_from_db()
            assert user.status == expected


# ---------------------------------------------------------------------------
# Location-scope foundation
# ---------------------------------------------------------------------------
class TestLocationScopeFoundation:
    def test_add_and_read(self, user, org_a, chain_a):
        membership = services.add_membership(user, org_a, actor=user)
        row = scope.add_membership_scope(membership, chain_a[1], actor=user)  # Region 1
        assert row.include_descendants is True
        assert scope.scope_roots(membership) == [row]
        assert ActivityLog.objects.filter(verb="membership_scope.added", actor=user).exists()

    def test_scoped_ids_expand_the_subtree_without_enforcing_anything(self, user, org_a, chain_a):
        membership = services.add_membership(user, org_a)
        scope.add_membership_scope(membership, chain_a[2])  # Area 1 + descendants
        assert scope.scoped_location_ids(membership) == {chain_a[2].pk, chain_a[3].pk, chain_a[4].pk}

    def test_without_descendants_only_the_root(self, user, org_a, chain_a):
        membership = services.add_membership(user, org_a)
        scope.add_membership_scope(membership, chain_a[2], include_descendants=False)
        assert scope.scoped_location_ids(membership) == {chain_a[2].pk}

    def test_idempotent_and_updatable(self, user, org_a, chain_a):
        membership = services.add_membership(user, org_a)
        first = scope.add_membership_scope(membership, chain_a[0])
        again = scope.add_membership_scope(membership, chain_a[0])
        assert first.pk == again.pk
        scope.add_membership_scope(membership, chain_a[0], include_descendants=False)
        first.refresh_from_db()
        assert first.include_descendants is False and MembershipLocationScope.objects.count() == 1

    def test_location_must_belong_to_the_memberships_organization(self, user, org_a, org_b, make_location):
        membership = services.add_membership(user, org_a)
        with pytest.raises(ValidationError):
            scope.add_membership_scope(membership, make_location(org_b, "Foreign"))
        assert not MembershipLocationScope.objects.exists()

    def test_database_trigger_blocks_cross_organization_rows(self, user, org_a, org_b, make_location):
        membership = services.add_membership(user, org_a)
        foreign = make_location(org_b, "Foreign")
        with pytest.raises(IntegrityError), transaction.atomic():
            MembershipLocationScope.objects.bulk_create([MembershipLocationScope(membership=membership, location=foreign)])

    def test_inactive_membership_cannot_be_scoped(self, user, org_a, chain_a):
        membership = services.add_membership(user, org_a)
        services.set_membership_status(membership, Status.SUSPENDED)
        with pytest.raises(ValidationError):
            scope.add_membership_scope(OrganizationMembership.objects.get(pk=membership.pk), chain_a[0])

    def test_remove_is_audited(self, user, org_a, chain_a):
        membership = services.add_membership(user, org_a)
        row = scope.add_membership_scope(membership, chain_a[0])
        scope.remove_membership_scope(row, actor=user)
        assert not MembershipLocationScope.objects.exists()
        assert ActivityLog.objects.filter(verb="membership_scope.removed").exists()

    def test_phase_boundary_scope_rows_do_not_restrict_anything_yet(self, client, make_user, org_a, chain_a):
        member = make_user(organization=org_a)
        scope.add_membership_scope(OrganizationMembership.objects.get(user=member), chain_a[4])
        client.force_login(member)
        assert client.get(reverse("locations:detail", args=[chain_a[0].pk])).status_code == 200


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------
class TestPreferences:
    def test_registry_is_an_allow_list(self):
        assert set(PREFERENCES) == {"timezone", "language"}
        for key, value in (("timezone", "Mars/Olympus"), ("language", "klingon!"), ("theme", "dark"), ("timezone", 5), ("timezone", "")):
            with pytest.raises(ValidationError):
                validate_preference(key, value)
        assert validate_preference("timezone", "Asia/Karachi") == "Asia/Karachi"
        assert validate_preference("language", " EN ") == "en"

    def test_set_change_clear_and_audit(self, user):
        services.set_preference(user, "timezone", "Asia/Karachi")
        services.set_preference(user, "timezone", "Europe/London")
        services.set_preference(user, "timezone", None)
        assert not UserPreference.objects.exists()
        changes = list(ActivityLog.objects.filter(verb="user.preference_changed").order_by("created_at").values_list("metadata", flat=True))
        assert [(c["from"], c["to"]) for c in changes] == [(None, "Asia/Karachi"), ("Asia/Karachi", "Europe/London"), ("Europe/London", None)]

    def test_unchanged_value_is_not_logged(self, user):
        services.set_preference(user, "language", "en")
        services.set_preference(user, "language", "en")
        assert ActivityLog.objects.filter(verb="user.preference_changed").count() == 1

    def test_unknown_key_is_rejected(self, user):
        with pytest.raises(ValidationError):
            services.set_preference(user, "anything", "x")

    def test_unique_per_user_and_key(self, user):
        UserPreference.objects.create(user=user, key="language", value="en")
        with pytest.raises(IntegrityError), transaction.atomic():
            UserPreference.objects.create(user=user, key="language", value="ur")

    def test_page_saves_and_validates(self, client, user):
        client.force_login(user)
        assert client.post(reverse("accounts:preferences"), {"timezone": "Asia/Karachi", "language": "en"}).status_code == 302
        assert UserPreference.objects.get(user=user, key="timezone").value == "Asia/Karachi"
        bad = client.post(reverse("accounts:preferences"), {"timezone": "Not/AZone", "language": ""})
        assert bad.status_code == 200 and UserPreference.objects.get(user=user, key="timezone").value == "Asia/Karachi"

    def test_api_document_and_patch(self, user):
        client = APIClient()
        client.force_authenticate(user)
        assert client.get("/api/v1/me/preferences/").json()["data"] == {"appearance": None, "timezone": None, "language": None}
        response = client.patch("/api/v1/me/preferences/", {"timezone": "Asia/Karachi", "appearance": "dark"}, format="json")
        assert response.status_code == 200
        assert response.json()["data"] == {"appearance": "dark", "timezone": "Asia/Karachi", "language": None}

    def test_api_rejects_unknown_and_invalid(self, user):
        client = APIClient()
        client.force_authenticate(user)
        assert client.patch("/api/v1/me/preferences/", {"theme_tokens": {}}, format="json").status_code == 400
        assert client.patch("/api/v1/me/preferences/", {"timezone": "Nope"}, format="json").status_code == 400
        assert client.patch("/api/v1/me/preferences/", {"appearance": "neon"}, format="json").status_code == 400
        assert client.patch("/api/v1/me/preferences/", {}, format="json").status_code == 400

    def test_api_requires_authentication_and_is_per_user(self, client, make_user, org_a):
        assert client.get("/api/v1/me/preferences/").status_code == 403
        one, two = make_user(organization=org_a), make_user(organization=org_a)
        api = APIClient()
        api.force_authenticate(one)
        api.patch("/api/v1/me/preferences/", {"language": "en"}, format="json")
        assert not UserPreference.objects.filter(user=two).exists()


# ---------------------------------------------------------------------------
# Identity unlink
# ---------------------------------------------------------------------------
class TestUnlink:
    def linked(self, **extra):
        return User.objects.create_user(email="linked@example.com", entra_object_id="oid-1", entra_tenant_id="tid-1", **extra)

    def test_unlink_without_a_usable_password_deactivates(self, staff_user):
        user = self.linked()
        assert not user.has_usable_password()
        result = services.unlink_external_identity(user, actor=staff_user, reason="left company")
        assert (result.entra_object_id, result.entra_tenant_id, result.status) == (None, "", UserStatus.INACTIVE)
        entry = ActivityLog.objects.get(verb="identity.unlinked")
        assert entry.actor == staff_user and entry.metadata["object_id"] == "oid-1" and entry.metadata["deactivated"] is True

    def test_unlink_keeps_an_account_that_can_still_sign_in(self, staff_user):
        user = self.linked(password="Str0ng-Passw0rd!")
        assert services.unlink_external_identity(user, actor=staff_user).status == UserStatus.ACTIVE

    def test_unlinked_identity_can_link_again_but_never_a_second_account(self):
        user = self.linked(password="Str0ng-Passw0rd!")
        services.unlink_external_identity(user)
        assert User.objects.filter(entra_object_id="oid-1").count() == 0

    def test_not_linked_is_an_error(self, user):
        with pytest.raises(ValidationError):
            services.unlink_external_identity(user)

    def test_admin_action(self, client, superuser):
        user = self.linked(password="Str0ng-Passw0rd!")
        client.force_login(superuser)
        client.post(reverse("admin:accounts_user_changelist"), {"action": "unlink_identities", "_selected_action": [str(user.pk)]}, follow=True)
        user.refresh_from_db()
        assert user.entra_object_id is None


# ---------------------------------------------------------------------------
# Session lifetime and PWA behaviour
# ---------------------------------------------------------------------------
def age_session(client, *, started=None, last=None):
    session = client.session
    now = time.time()
    session[AUTH_STARTED_KEY] = now - (started if started is not None else 10)
    session[LAST_ACTIVITY_KEY] = now - (last if last is not None else 10)
    session.save()


class TestSessionLifetime:
    @pytest.fixture(autouse=True)
    def _limits(self, settings):
        settings.SESSION_IDLE_TIMEOUT_SECONDS = 600
        settings.SESSION_ABSOLUTE_TIMEOUT_SECONDS = 3600

    def test_active_session_works_and_is_stamped(self, client, user):
        client.force_login(user)
        assert client.get(reverse("accounts:profile")).status_code == 200
        assert AUTH_STARTED_KEY in client.session and LAST_ACTIVITY_KEY in client.session

    def test_idle_session_page_redirects_to_login_with_a_message(self, client, user):
        client.force_login(user)
        client.get(reverse("accounts:profile"))
        age_session(client, last=601)
        response = client.get(reverse("accounts:profile"))
        assert response.status_code == 302 and reverse("accounts:login") in response["Location"]
        assert client.session.get("_auth_user_id") is None
        assert "session expired" in client.get(reverse("accounts:login")).content.decode().lower()

    def test_absolute_limit_ends_even_an_active_session(self, client, user):
        client.force_login(user)
        client.get(reverse("accounts:profile"))
        age_session(client, started=3601, last=1)
        assert client.get(reverse("accounts:profile")).status_code == 302
        assert client.session.get("_auth_user_id") is None

    def test_expired_api_call_gets_json_401(self, client, user):
        client.force_login(user)
        client.get(reverse("accounts:profile"))
        age_session(client, last=601)
        response = client.get("/api/v1/me/")
        assert response.status_code == 401 and response["Cache-Control"] == "no-store"
        assert response.json()["error"]["code"] == SESSION_EXPIRED_CODE

    def test_expired_htmx_call_redirects_the_browser(self, client, user):
        client.force_login(user)
        client.get(reverse("accounts:profile"))
        age_session(client, last=601)
        response = client.get(reverse("accounts:profile"), HTTP_HX_REQUEST="true")
        assert response.status_code == 401
        assert reverse("accounts:login") in response["HX-Redirect"] and "expired=1" in response["HX-Redirect"]

    def test_unauthenticated_api_is_not_reported_as_expired(self, client, db):
        assert client.get("/api/v1/me/").status_code == 403

    def test_idle_stamp_is_not_rewritten_on_every_request(self, client, user):
        client.force_login(user)
        client.get(reverse("accounts:profile"))
        stamp = client.session[LAST_ACTIVITY_KEY]
        client.get(reverse("accounts:profile"))
        assert client.session[LAST_ACTIVITY_KEY] == stamp  # within the 60 s write interval

    def test_stale_stamp_is_refreshed_by_activity(self, client, user):
        client.force_login(user)
        client.get(reverse("accounts:profile"))
        age_session(client, last=120)
        client.get(reverse("accounts:profile"))
        assert time.time() - client.session[LAST_ACTIVITY_KEY] < 5

    def test_probe_reports_state_without_extending_the_session(self, client, user):
        client.force_login(user)
        client.get(reverse("accounts:profile"))
        age_session(client, last=120)
        before = client.session[LAST_ACTIVITY_KEY]
        body = client.get(reverse("accounts:session"))
        assert body.json()["authenticated"] is True and 0 < body.json()["expires_in"] <= 600
        assert body["Cache-Control"] == "no-store"
        assert client.session[LAST_ACTIVITY_KEY] == before

    def test_probe_for_anonymous(self, client, db):
        assert client.get(reverse("accounts:session")).json() == {"authenticated": False, "expires_in": None}

    def test_idle_timeout_can_be_disabled(self, client, user, settings):
        settings.SESSION_IDLE_TIMEOUT_SECONDS = 0
        client.force_login(user)
        client.get(reverse("accounts:profile"))
        age_session(client, last=100000)
        assert client.get(reverse("accounts:profile")).status_code == 200

    def test_login_logout_pages_do_not_loop(self, client, user):
        client.force_login(user)
        client.get(reverse("accounts:profile"))
        age_session(client, last=601)
        first = client.get(reverse("accounts:profile"), follow=True)
        assert first.redirect_chain[-1][0].startswith(reverse("accounts:login")) and first.status_code == 200

    def test_service_worker_never_caches_authentication_routes(self, client, db):
        source = client.get("/service-worker.js").content.decode()
        assert '"/auth/"' in source and 'cp-static-v7' in source


# ---------------------------------------------------------------------------
# Hardening
# ---------------------------------------------------------------------------
class TestHardening:
    def run_settings(self, module, **env):
        base = {**os.environ, "SECRET_KEY": "x" * 60, "DATABASE_URL": os.environ.get("DATABASE_URL", "postgres://u:p@localhost/db"),
                "DJANGO_ALLOWED_HOSTS": "example.com", "DJANGO_SETTINGS_MODULE": module, **env}
        code = ("import django; from django.conf import settings; "
                "print(settings.ENABLE_PASSWORD_LOGIN, settings.SESSION_COOKIE_SECURE, settings.CSRF_COOKIE_SECURE, "
                "settings.SESSION_COOKIE_HTTPONLY, settings.SESSION_COOKIE_SAMESITE, settings.CSRF_COOKIE_SAMESITE, settings.SECURE_HSTS_SECONDS)")
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=base, cwd=os.getcwd(), timeout=60)

    def test_production_refuses_to_start_with_password_login(self):
        result = self.run_settings("config.settings.production", ENABLE_PASSWORD_LOGIN="true")
        assert result.returncode != 0 and "ENABLE_PASSWORD_LOGIN must be false" in result.stderr

    def test_production_starts_without_it_and_cookies_are_hardened(self):
        result = self.run_settings("config.settings.production", ENABLE_PASSWORD_LOGIN="false")
        assert result.returncode == 0, result.stderr
        password, sess_secure, csrf_secure, httponly, sess_site, csrf_site, hsts = result.stdout.split()
        assert (password, sess_secure, csrf_secure, httponly, sess_site, csrf_site) == ("False", "True", "True", "True", "Lax", "Lax")
        assert int(hsts) >= 31536000

    def test_throttle_ignores_a_forged_forwarded_header_by_default(self, rf, settings):
        settings.TRUSTED_PROXY_COUNT = 0
        request = rf.get("/", HTTP_X_FORWARDED_FOR="6.6.6.6", REMOTE_ADDR="10.0.0.5")
        assert client_address(request) == "10.0.0.5"

    def test_throttle_reads_the_hop_added_by_the_trusted_proxy(self, rf, settings):
        settings.TRUSTED_PROXY_COUNT = 1
        forged = rf.get("/", HTTP_X_FORWARDED_FOR="6.6.6.6, 203.0.113.9", REMOTE_ADDR="10.0.0.5")
        assert client_address(forged) == "203.0.113.9"  # the client-supplied 6.6.6.6 is not believed
        short = rf.get("/", HTTP_X_FORWARDED_FOR="", REMOTE_ADDR="10.0.0.5")
        assert client_address(short) == "10.0.0.5"

    @pytest.mark.parametrize("code", ["invalid_state", "authentication_failed", "access_denied", "invalid_request",
                                       "invalid_client", "membership_missing", "user_not_allowed", "tenant_not_allowed",
                                       "account_suspended", "account_inactive", "account_pending"])
    def test_every_failure_has_a_safe_message(self, code):
        message = REASON_MESSAGES[code]
        for leak in ("secret", "token", "traceback", "client_id", "tenant id", "http"):
            assert leak not in message.lower()

    def test_cookie_settings_in_every_environment(self, settings):
        assert settings.SESSION_COOKIE_HTTPONLY is True
        assert settings.SESSION_COOKIE_SAMESITE == "Lax" and settings.CSRF_COOKIE_SAMESITE == "Lax"


# ---------------------------------------------------------------------------
# Cross-organization security (web + API)
# ---------------------------------------------------------------------------
class TestCrossOrganizationSecurity:
    def test_forged_organization_on_switch_is_a_404_and_context_is_unchanged(self, client, admin_a, org_b):
        client.force_login(admin_a)
        client.post(reverse("orgcontext:switch"), {"organization": str(org_b.pk)})
        assert client.session.get(ACTIVE_ORGANIZATION_SESSION_KEY) != str(org_b.pk)
        assert client.post(reverse("orgcontext:switch"), {"organization": str(org_b.pk)}).status_code == 404

    def test_query_string_cannot_select_an_organization(self, client, admin_a, org_b):
        client.force_login(admin_a)
        body = client.get("/organizations/", {"organization_id": str(org_b.pk), "organization": str(org_b.pk)}).content.decode()
        assert "Org B" not in body

    def test_session_pointing_at_a_lost_membership_is_recalculated(self, client, make_user, org_a, org_b):
        user = make_user(organization=org_a)
        services.add_membership(user, org_b)
        client.force_login(user)
        client.post(reverse("orgcontext:switch"), {"organization": str(org_b.pk)})
        services.remove_membership(OrganizationMembership.objects.get(user=user, organization=org_b))
        body = client.get(reverse("accounts:preferences")).content.decode()
        assert "Org B" not in body.split("Active organization")[1].split("Organization memberships")[0]

    def test_api_users_filter_cannot_reach_another_organization(self, admin_a, admin_b, org_b):
        api = APIClient()
        api.force_authenticate(admin_a)
        emails = {row["email"] for row in api.get("/api/v1/users/", {"organization": str(org_b.pk)}).json()["data"]}
        assert admin_b.email not in emails

    def test_forged_user_id_is_not_found(self, admin_a, admin_b):
        api = APIClient()
        api.force_authenticate(admin_a)
        assert api.get(f"/api/v1/users/{admin_b.pk}/").status_code == 404

    def test_api_organization_detail_needs_a_membership(self, admin_a, org_b):
        api = APIClient()
        api.force_authenticate(admin_a)
        assert api.get(f"/api/v1/organizations/{org_b.pk}/").status_code == 404

    @pytest.mark.parametrize("status", [Status.SUSPENDED, Status.LEFT])
    def test_inactive_membership_loses_web_and_api_access(self, client, admin_a, org_a, status):
        services.set_membership_status(OrganizationMembership.objects.get(user=admin_a), status)
        client.force_login(admin_a)
        assert client.get(reverse("organizations:detail", args=[org_a.pk])).status_code == 404
        api = APIClient()
        api.force_authenticate(admin_a)
        assert api.get(f"/api/v1/organizations/{org_a.pk}/").status_code == 404

    def test_suspended_user_is_denied_the_api_and_signed_out(self, client, admin_a):
        client.force_login(admin_a)
        services.suspend_user(admin_a)
        assert client.get("/api/v1/me/").status_code in (401, 403)
        assert client.session.get("_auth_user_id") is None

    def test_csrf_is_enforced_on_state_changes(self, admin_a, org_a):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(admin_a)
        assert strict.post(reverse("orgcontext:switch"), {"organization": str(org_a.pk)}).status_code == 403
        assert strict.post(reverse("accounts:logout")).status_code == 403
        assert strict.post(reverse("accounts:preferences"), {"timezone": "Asia/Karachi"}).status_code == 403

    def test_logout_requires_post(self, client, user):
        client.force_login(user)
        assert client.get(reverse("accounts:logout")).status_code == 405

    @pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example/", "javascript:alert(1)"])
    def test_unsafe_post_login_redirects_are_refused(self, client, user, org_a, target):
        services.add_membership(user, org_a)
        client.force_login(user)
        response = client.post(reverse("orgcontext:switch"), {"organization": str(org_a.pk), "next": target})
        assert "evil.example" not in response["Location"] and "javascript" not in response["Location"]


# ---------------------------------------------------------------------------
# Theme integration
# ---------------------------------------------------------------------------
class TestThemeIntegration:
    @pytest.mark.parametrize("mode", ["light", "dark", "system"])
    def test_account_preference_drives_the_rendered_page(self, client, user, mode):
        client.force_login(user)
        client.post(reverse("accounts:appearance"), {"mode": mode})
        for path in ("/", reverse("accounts:preferences"), reverse("accounts:profile")):
            assert f'data-theme="{mode}"' in client.get(path).content.decode()

    def test_login_page_follows_the_visitors_cookie_and_uses_semantic_tokens(self, client, db):
        client.cookies["cp_appearance"] = "dark"
        html = client.get(reverse("accounts:login")).content.decode()
        assert 'data-theme="dark"' in html and "/themes/tokens.css" in html
        import re

        assert not re.search(r"(?:bg|text|border)-(?:slate|indigo|red|blue)-\d", html)

    def test_user_choice_beats_the_organization_default(self, client, org_a, make_theme, make_user):
        make_theme(org_a, "Light org", status="active", default_appearance="light")
        member = make_user(organization=org_a)
        client.force_login(member)
        assert 'data-theme="light"' in client.get("/").content.decode()
        client.post(reverse("accounts:appearance"), {"mode": "dark"})
        assert 'data-theme="dark"' in client.get("/").content.decode()
        theme_services.set_user_appearance(member, None)
        assert 'data-theme="light"' in client.get("/").content.decode()

    def test_organization_theme_reaches_account_pages(self, client, org_a, make_theme, make_user):
        make_theme(org_a, "Brand", status="active", tokens={"light": {"primary": "#123456"}})
        client.force_login(make_user(organization=org_a))
        assert "#123456" in client.get("/themes/tokens.css").content.decode()


# ---------------------------------------------------------------------------
# Performance budgets
# ---------------------------------------------------------------------------
class TestQueryBudgets:
    def test_authenticated_dashboard_shell(self, client, admin_a, django_assert_max_num_queries):
        client.force_login(admin_a)
        client.get("/")  # warm caches (content types, sessions, RBAC grants)
        with django_assert_max_num_queries(14):  # +2: the scope-aware location selector
            assert client.get("/").status_code == 200

    def test_account_page(self, client, admin_a, django_assert_max_num_queries):
        client.force_login(admin_a)
        client.get(reverse("accounts:profile"))
        with django_assert_max_num_queries(14):
            assert client.get(reverse("accounts:profile")).status_code == 200

    def test_organization_switcher(self, client, make_user, org_a, org_b, django_assert_max_num_queries):
        user = make_user(organization=org_a)
        services.add_membership(user, org_b)
        client.force_login(user)
        client.get(reverse("orgcontext:select"))
        with django_assert_max_num_queries(14):
            assert client.get(reverse("orgcontext:select")).status_code == 200

    def test_api_me_does_not_resolve_organization_context(self, admin_a, django_assert_max_num_queries):
        api = APIClient()
        api.force_authenticate(admin_a)
        api.get("/api/v1/me/")
        with django_assert_max_num_queries(4):
            assert api.get("/api/v1/me/").status_code == 200

    def test_login_redirect_completion(self, client, user, django_assert_max_num_queries):
        client.force_login(user)
        with django_assert_max_num_queries(8):
            assert client.get(reverse("accounts:login")).status_code in (200, 302)


# ---------------------------------------------------------------------------
# Every account page renders (regression: profile_edit.html was missing)
# ---------------------------------------------------------------------------
class TestAccountPagesRender:
    @pytest.mark.parametrize("name", ["accounts:profile", "accounts:profile_edit", "accounts:preferences",
                                      "accounts:appearance", "orgcontext:select"])
    def test_page_renders(self, client, user, org_a, name):
        services.add_membership(user, org_a, is_default=True)
        client.force_login(user)
        response = client.get(reverse(name))
        assert response.status_code == 200
        html = response.content.decode()
        assert "{{" not in html and "{%" not in html

    def test_profile_edit_shows_only_application_managed_fields(self, client, user):
        client.force_login(user)
        html = client.get(reverse("accounts:profile_edit")).content.decode()
        assert 'name="phone_number"' in html and 'name="avatar"' in html
        for idp_owned in ('name="email"', 'name="display_name"', 'name="status"', 'name="entra_object_id"'):
            assert idp_owned not in html
        assert 'enctype="multipart/form-data"' in html

    def test_every_template_named_by_a_view_exists(self):
        import re
        from pathlib import Path

        from django.template.loader import get_template

        source = Path("apps/accounts/views.py").read_text(encoding="utf-8")
        for name in set(re.findall(r'template_name = "([^"]+)"', source)):
            get_template(name)

